#!/usr/bin/env python3
"""Model health checker — runs via cron, tests LLM provider models and writes results to SQLite.

A provider whose free tier ends answers a terminal code on every model. Such a provider is
parked for 24h and knocked on with a single model per run instead of the full sweep.
"""
import json
import logging
import os
import urllib.request
import urllib.error
import sqlite3
import sys
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Sequence

DB_FILE = "/var/lib/telegram-llm-bot.db"
PROXY_FILE = "/etc/socks-monitor/.proxy_url"
CONFIG_FILE = os.environ.get("BOT_CONFIG", "/etc/socks-monitor/config.json")
ADMIN_FILE = os.environ.get("BOT_ADMIN_FILE", "/etc/socks-monitor/.admin_id")


def _resolve_proxy():
    if os.environ.get("BOT_PROXY_DISABLED", "").strip():
        return ""
    val = os.environ.get("BOT_PROXY_URL", "")
    if val:
        return val
    try:
        return Path(PROXY_FILE).read_text().strip()
    except Exception:
        return ""


PROXY_URL = _resolve_proxy()
USE_PROXY = not os.environ.get("BOT_PROXY_DISABLED", "").strip()

HEALTH_CHECK_PROMPT = "Ответь одним словом: столица Франции?"
HEALTH_CHECK_MAX_TOKENS = 10
HEALTH_CHECK_TIMEOUT = 30
WORKERS = 10
OPENROUTER_DELAY_SEC = 1.5

# 401/402/403 is the provider saying "not for you any more". 429 and 5xx say "not right now",
# and a network error says nothing about the provider at all — neither may park anyone.
TERMINAL_HTTP_CODES = frozenset({401, 402, 403})
DEAD_RUN_TERMINAL_SHARE = 0.9
DEAD_RUNS_BEFORE_DISABLE = 3
DISABLE_SEC = 24 * 3600
PROBE_LOOKBACK_SEC = 7 * 86400
TEXTUAL_CATEGORIES = ("text", "code")

PROVIDERS = {
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "models_url": "https://shir-man.com/api/free-llm/top-models",
        "key_file": "/etc/socks-monitor/.openrouter_key",
        "supports_tools": True,
        "proxy": False,
        "models_format": "top-models",
        "health_timeout": 60,
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models_url": "https://api.groq.com/openai/v1/models",
        "key_file": "/etc/socks-monitor/.groq_key",
        "supports_tools": True,
        "proxy": USE_PROXY,
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "models_url": "https://api.cerebras.ai/v1/models",
        "key_file": "/etc/socks-monitor/.cerebras_key",
        "supports_tools": False,
        "proxy": USE_PROXY,
    },
    "nvidia": {
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "key_file": "/etc/socks-monitor/.nvidia_key",
        "supports_tools": True,
        "proxy": False,
    },
}

_CAT_RULES = [
    ("image",       ["image", "visual", "flux", "stable-diffusion", "sdxl", "cosmos", "transfer"]),
    ("video",       ["video", "stream", "speaker-detection"]),
    ("audio",       ["speech", "voicechat", "riva", "whisper", "tts"]),
    ("embedding",   ["embed", "retriever", "rerank"]),
    ("safety",      ["safety", "guard", "pii", "content-safety"]),
    ("code",        ["coder", "codestral", "devstral", "starcoder"]),
    ("translation", ["translate"]),
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)


class ModelProbe(NamedTuple):
    """Result of knocking on one model."""
    model_id: str
    latency_ms: int
    available: bool
    supports_tools: bool
    category: str
    error: str | None = None
    rate_limited: bool = False
    http_code: int | None = None
    message: str = ""


class ProviderState(NamedTuple):
    """Backoff state of one provider, mirroring the provider_state row."""
    disabled_until: int = 0
    reason: str = ""
    consecutive_dead_runs: int = 0
    updated_ts: int = 0


def shutdown_evidence(probes: Sequence[ModelProbe]) -> tuple[int, str] | None:
    """(code, provider message) if this run looks like the provider closed the door, else None.

    Closed means: nothing answered, and nearly every attempt came back with a terminal code.
    A run where anything still works, or where the failures are 429/5xx/network, proves nothing.
    """
    attempts = [p for p in probes if p.category in TEXTUAL_CATEGORIES]
    if not attempts:
        return None
    if any(p.available for p in attempts):
        return None
    terminal = [p for p in attempts if p.http_code in TERMINAL_HTTP_CODES]
    if len(terminal) / len(attempts) < DEAD_RUN_TERMINAL_SHARE:
        return None
    codes = [p.http_code for p in terminal]
    code = max(set(codes), key=codes.count)
    message = next((p.message for p in terminal if p.http_code == code and p.message), "")
    return code, message


def next_state_after_run(prev: ProviderState, probes: Sequence[ModelProbe], now: int) -> tuple[ProviderState, str]:
    """New state and event ("disabled" | "recovered" | "") after a full sweep."""
    attempts = len([p for p in probes if p.category in TEXTUAL_CATEGORIES])
    evidence = shutdown_evidence(probes)
    if evidence is None:
        if not attempts:
            return prev, ""
        event = "recovered" if prev.disabled_until > now else ""
        return ProviderState(0, "", 0, now), event
    code, message = evidence
    runs = prev.consecutive_dead_runs + 1
    if runs < DEAD_RUNS_BEFORE_DISABLE or prev.disabled_until > now:
        return ProviderState(prev.disabled_until, prev.reason, runs, now), ""
    reason = f"HTTP {code} on all {attempts} models: {message}".strip()[:300]
    return ProviderState(now + DISABLE_SEC, reason, runs, now), "disabled"


def next_state_after_probe(prev: ProviderState, probe: ModelProbe | None, now: int) -> tuple[ProviderState, str]:
    """New state and event after the single knock that replaces the sweep while parked."""
    if probe is not None and probe.available:
        return ProviderState(0, "", 0, now), "recovered"
    return ProviderState(prev.disabled_until, prev.reason, prev.consecutive_dead_runs, now), ""


def categorize_model(model_id):
    mid = model_id.lower()
    for cat, keywords in _CAT_RULES:
        if any(kw in mid for kw in keywords):
            return cat
    return "text"


def capabilities_for_category(category):
    if category in ("text", "code"):
        return "text"
    if category == "audio":
        return "audio"
    if category == "image":
        return "image"
    if category == "video":
        return "video"
    if category == "embedding":
        return "embedding"
    if category == "translation":
        return "translation"
    if category == "safety":
        return "safety"
    return ""


def load_key(provider_name):
    try:
        return Path(PROVIDERS[provider_name]["key_file"]).read_text().strip()
    except Exception:
        return ""


def read_error_body(err: urllib.error.HTTPError, limit: int = 200) -> str:
    """The provider's own words — the only place that says *why* the door is shut."""
    try:
        return " ".join(err.read().decode("utf-8", errors="ignore").split())[:limit]
    except Exception:
        return ""


def make_opener(use_proxy):
    if use_proxy and PROXY_URL:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": PROXY_URL, "http": PROXY_URL}))
    return urllib.request.build_opener()


def fetch_models(provider_name):
    prov = PROVIDERS[provider_name]
    api_key = load_key(provider_name)
    headers = {"User-Agent": "Mozilla/5.0"}
    if prov.get("models_format") != "top-models":
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(prov["models_url"], headers=headers)
    opener = make_opener(prov.get("proxy", False))
    with opener.open(req, timeout=10) as resp:
        raw = json.loads(resp.read().decode())
        if prov.get("models_format") == "top-models":
            # shir-man.com returns {"models": [{"id": "..."}]} or list
            items = raw if isinstance(raw, list) else raw.get("models", raw.get("data", []))
        else:
            items = raw.get("data", [])
        seen = set()
        models = []
        for m in items:
            mid = m.get("id") if isinstance(m, dict) else m
            if mid and mid not in seen:
                seen.add(mid)
                models.append(mid)
        return models

def _carry_or_set_availability(conn, prov_name, model_id, fresh_available, rate_limited):
    """Decide what to write to model_health.available.

    On rate-limit, keep whatever was there before — temporary 429 from a shared key isn't a real
    'model is unavailable' signal. On any other outcome, write the fresh result. New models default
    to 1 on rate-limit so they don't get hidden on first encounter."""
    if not rate_limited:
        return 1 if fresh_available else 0
    row = conn.execute(
        "SELECT available FROM model_health WHERE provider = ? AND model_id = ?",
        (prov_name, model_id)).fetchone()
    return row[0] if row is not None else 1


def check_model(prov_name, prov, api_key, model_id):
    """Check a single model, returning a ModelProbe.

    rate_limited=True means the probe hit HTTP 429 and the model's `available` flag should be left as it was —
    a temporary key-quota exhaustion is not the same as the model being broken."""
    category = categorize_model(model_id)
    if category == "audio":
        return ModelProbe(model_id, 0, False, prov.get("supports_tools", False), "audio")
    if category not in ("text", "code"):
        return ModelProbe(model_id, 0, False, prov.get("supports_tools", False), category)
    try:
        opener = make_opener(prov.get("proxy", False))
        timeout = prov.get("health_timeout", HEALTH_CHECK_TIMEOUT)
        start = time.time()
        payload = {"model": model_id,
                   "messages": [{"role": "user", "content": HEALTH_CHECK_PROMPT}],
                   "max_tokens": HEALTH_CHECK_MAX_TOKENS}
        req = urllib.request.Request(
            prov["url"], json.dumps(payload).encode(),
            {"Content-Type": "application/json",
             "Authorization": f"Bearer {api_key}",
             "User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=timeout) as f:
            json.loads(f.read().decode())
            latency = int((time.time() - start) * 1000)
            log.info(f"  ✅ {model_id}: {latency}ms")
            return ModelProbe(model_id, latency, True, prov.get("supports_tools", False), category)
    except urllib.error.HTTPError as e:
        err = f"HTTP Error {e.code}: {e.reason}"
        rate_limited = (e.code == 429)
        log.info(f"  {'⏸' if rate_limited else '❌'} {model_id}: {err}")
        return ModelProbe(model_id, 0, False, False, category, err, rate_limited, e.code, read_error_body(e))
    except Exception as e:
        err = str(e)[:200]
        log.info(f"  ❌ {model_id}: {e}")
        return ModelProbe(model_id, 0, False, False, category, err)


def fetch_openrouter_model_list(prov):
    """Fetch model list from shir-man.com top-models, extract IDs and metadata."""
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(prov["models_url"], headers=headers)
    opener = make_opener(prov.get("proxy", False))
    with opener.open(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode())
    models = raw.get("models", [])
    result = []
    for m in models:
        mid = m.get("id", "")
        if mid:
            result.append({
                "id": mid,
                "supportsTools": bool(m.get("supportsTools")),
            })
    return result


def load_provider_state(conn, prov_name) -> ProviderState:
    try:
        row = conn.execute(
            "SELECT disabled_until, reason, consecutive_dead_runs, updated_ts FROM provider_state WHERE provider = ?",
            (prov_name,)).fetchone()
    except Exception as e:
        log.error(f"load_provider_state({prov_name}): {e}")
        return ProviderState()
    if not row:
        return ProviderState()
    return ProviderState(row[0] or 0, row[1] or "", row[2] or 0, row[3] or 0)


def save_provider_state(conn, prov_name, state: ProviderState) -> None:
    try:
        conn.execute(
            """INSERT INTO provider_state (provider, disabled_until, reason, consecutive_dead_runs, updated_ts)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(provider) DO UPDATE SET
                   disabled_until=excluded.disabled_until,
                   reason=excluded.reason,
                   consecutive_dead_runs=excluded.consecutive_dead_runs,
                   updated_ts=excluded.updated_ts""",
            (prov_name, state.disabled_until, state.reason, state.consecutive_dead_runs, state.updated_ts))
        conn.commit()
    except Exception as e:
        log.error(f"save_provider_state({prov_name}): {e}")


def notify_admin(text: str) -> None:
    """Best-effort note to the admin. No token on this host is normal — stay quiet then."""
    try:
        cfg = json.loads(Path(CONFIG_FILE).read_text())
        token = Path(cfg["bot_token_file"]).read_text().strip()
        admin_id = int(Path(ADMIN_FILE).read_text().strip())
    except Exception:
        return
    if not token or not admin_id:
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json.dumps({"chat_id": admin_id, "text": text}).encode(),
            {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log.warning(f"admin notify failed: {e}")


def announce(prov_name: str, state: ProviderState, event: str) -> None:
    if event == "disabled":
        until = datetime.fromtimestamp(state.disabled_until, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        log.warning(f"{prov_name}: shut its door ({state.reason}) — parked until {until}, one probe per run")
        notify_admin(f"🚫 {prov_name} убран из обстрела до {until}\n{state.reason}\n"
                     f"Проверяю одной моделью раз в прогон.")
    elif event == "recovered":
        log.info(f"{prov_name}: answering again — back in the full sweep")
        notify_admin(f"✅ {prov_name} снова отвечает — возвращён в полный обстрел.")


def apply_run_state(conn, prov_name, probes, prev: ProviderState, now: int) -> None:
    """Fold one full sweep into provider_state — one log line per run, not per model."""
    state, event = next_state_after_run(prev, probes, now)
    if (state.disabled_until, state.reason, state.consecutive_dead_runs) == \
            (prev.disabled_until, prev.reason, prev.consecutive_dead_runs):
        return
    save_provider_state(conn, prov_name, state)
    if event:
        announce(prov_name, state, event)
    else:
        log.info(f"{prov_name}: dead-run streak {prev.consecutive_dead_runs} → "
                 f"{state.consecutive_dead_runs}/{DEAD_RUNS_BEFORE_DISABLE}")


def pick_probe_model(conn, prov_name) -> str | None:
    """Fastest model from the provider's last living sweep; any known model if none ever lived.

    The category join matters: the audio and media crons log into the same table, and an audio
    model would fail this text probe forever and never let the provider back in.
    """
    since = int(time.time()) - PROBE_LOOKBACK_SEC
    try:
        row = conn.execute(
            "SELECT l.model_id FROM model_health_log l "
            "JOIN model_health h ON h.provider = l.provider AND h.model_id = l.model_id "
            "WHERE l.provider = ? AND l.available = 1 AND l.ts >= ? AND h.category IN ('text', 'code') "
            "ORDER BY l.ts DESC, l.latency_ms ASC LIMIT 1", (prov_name, since)).fetchone()
        if row:
            return row[0]
        row = conn.execute(
            "SELECT model_id FROM model_health WHERE provider = ? AND category IN ('text', 'code') "
            "ORDER BY model_id LIMIT 1", (prov_name,)).fetchone()
        if row:
            return row[0]
    except Exception as e:
        log.error(f"pick_probe_model({prov_name}): {e}")
    try:
        for mid in fetch_models(prov_name):
            if categorize_model(mid) in TEXTUAL_CATEGORIES:
                return mid
    except Exception as e:
        log.error(f"pick_probe_model fetch({prov_name}): {e}")
    return None


def probe_parked_provider(conn, prov_name, prev: ProviderState, now: int) -> None:
    """One knock instead of ~130 — the whole point of parking a provider that shut its door."""
    left_min = max(0, (prev.disabled_until - now) // 60)
    api_key = load_key(prov_name)
    model_id = pick_probe_model(conn, prov_name) if api_key else None
    if not model_id:
        log.info(f"{prov_name}: parked for another {left_min} min, no model to probe")
        return
    probe = check_model(prov_name, PROVIDERS[prov_name], api_key, model_id)
    conn.execute(
        "INSERT INTO model_health_log (ts, provider, model_id, latency_ms, available, error) VALUES (?, ?, ?, ?, ?, ?)",
        (now, prov_name, model_id, probe.latency_ms, 1 if probe.available else 0, probe.error))
    conn.commit()
    state, event = next_state_after_probe(prev, probe, now)
    save_provider_state(conn, prov_name, state)
    if event:
        announce(prov_name, state, event)
    else:
        log.info(f"{prov_name}: parked for another {left_min} min, probe {model_id} → {probe.error}")


def check_provider(conn, prov_name, openrouter_delay_sec=OPENROUTER_DELAY_SEC):
    started = int(time.time())
    prev = load_provider_state(conn, prov_name)
    if prev.disabled_until > started:
        probe_parked_provider(conn, prov_name, prev, started)
        return
    return sweep_provider(conn, prov_name, prev, openrouter_delay_sec)


def sweep_provider(conn, prov_name, prev, openrouter_delay_sec=OPENROUTER_DELAY_SEC):
    if prov_name == "openrouter":
        prov = PROVIDERS[prov_name]
        api_key = load_key(prov_name)
        if not api_key:
            log.warning(f"No API key for {prov_name}, skipping")
            return
        try:
            models_meta = fetch_openrouter_model_list(prov)
        except Exception as e:
            log.error(f"fetch openrouter model list: {e}")
            return
        models = [m["id"] for m in models_meta]
        tools_map = {m["id"]: m["supportsTools"] for m in models_meta}
        prov_copy = dict(prov)

        ok, fail, throttled = 0, 0, 0
        log.info(f"Checking {prov_name}: {len(models)} models (sequential, delay={openrouter_delay_sec}s)")

        # Phase 1: run all HTTP probes OUTSIDE any SQLite transaction.
        results = []
        for i, mid in enumerate(models):
            result = check_model(prov_name, prov_copy, api_key, mid)
            results.append(result)
            if result.rate_limited:
                throttled += 1
            elif result.available:
                ok += 1
            elif result.category in TEXTUAL_CATEGORIES:
                fail += 1
            if i < len(models) - 1 and openrouter_delay_sec > 0:
                time.sleep(openrouter_delay_sec)

        # Phase 2: batch commit all results in a single short transaction.
        now = int(time.time())
        for p in results:
            if p.category not in TEXTUAL_CATEGORIES:
                continue
            supports_tools = tools_map.get(p.model_id, prov.get("supports_tools", False))
            effective_available = _carry_or_set_availability(conn, prov_name, p.model_id, p.available, p.rate_limited)
            conn.execute(
                "INSERT OR REPLACE INTO model_health (provider, model_id, latency_ms, available, supports_tools, category, capabilities, last_check) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (prov_name, p.model_id, p.latency_ms, effective_available,
                 1 if supports_tools else 0, p.category, capabilities_for_category(p.category), now))
            conn.execute(
                "INSERT INTO model_health_log (ts, provider, model_id, latency_ms, available, error) VALUES (?, ?, ?, ?, ?, ?)",
                (now, prov_name, p.model_id, p.latency_ms, 1 if p.available else 0, p.error))
        conn.commit()
        log.info(f"{prov_name}: {ok} ok, {fail} failed, {throttled} rate-limited (kept prior state)")
        apply_run_state(conn, prov_name, results, prev, now)
        return

    prov = PROVIDERS[prov_name]
    api_key = load_key(prov_name)
    if not api_key:
        log.warning(f"No API key for {prov_name}, skipping")
        return

    try:
        models = fetch_models(prov_name)
    except Exception as e:
        log.error(f"fetch_models({prov_name}): {e}")
        return

    ok, fail, throttled = 0, 0, 0
    log.info(f"Checking {prov_name}: {len(models)} models ({WORKERS} workers)")

    # Phase 1: run all HTTP probes in parallel, OUTSIDE any SQLite transaction.
    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(check_model, prov_name, prov, api_key, mid): mid for mid in models}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if result.rate_limited:
                throttled += 1
            elif result.available:
                ok += 1
            elif result.category in TEXTUAL_CATEGORIES:
                fail += 1

    # Phase 2: batch commit all results in a single short transaction.
    now = int(time.time())
    for p in results:
        if p.category not in TEXTUAL_CATEGORIES:
            continue
        effective_available = _carry_or_set_availability(conn, prov_name, p.model_id, p.available, p.rate_limited)
        conn.execute(
            "INSERT OR REPLACE INTO model_health (provider, model_id, latency_ms, available, supports_tools, category, capabilities, last_check) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (prov_name, p.model_id, p.latency_ms, effective_available,
             1 if p.supports_tools else 0, p.category, capabilities_for_category(p.category), now))
        conn.execute(
            "INSERT INTO model_health_log (ts, provider, model_id, latency_ms, available, error) VALUES (?, ?, ?, ?, ?, ?)",
            (now, prov_name, p.model_id, p.latency_ms, 1 if p.available else 0, p.error))
    conn.commit()
    log.info(f"{prov_name}: {ok} ok, {fail} failed, {throttled} rate-limited (kept prior state)")
    apply_run_state(conn, prov_name, results, prev, now)


def main():
    parser = argparse.ArgumentParser(description="Check model health for one provider or all providers")
    parser.add_argument(
        "--provider",
        default="all",
        choices=["all", *PROVIDERS.keys()],
        help="Provider to check (default: all)",
    )
    parser.add_argument(
        "--openrouter-delay",
        type=float,
        default=OPENROUTER_DELAY_SEC,
        help=f"Delay in seconds between OpenRouter model checks (default: {OPENROUTER_DELAY_SEC})",
    )
    args = parser.parse_args()
    providers_to_check = list(PROVIDERS.keys()) if args.provider == "all" else [args.provider]

    with sqlite3.connect(DB_FILE, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        for prov_name in providers_to_check:
            try:
                check_provider(conn, prov_name, openrouter_delay_sec=args.openrouter_delay)
            except Exception as e:
                log.error(f"Error checking {prov_name}: {e}")


if __name__ == "__main__":
    main()
