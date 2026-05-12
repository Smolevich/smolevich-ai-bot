#!/usr/bin/env python3
"""Model health checker — runs via cron, tests LLM provider models and writes results to SQLite."""
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
from pathlib import Path

DB_FILE = "/var/lib/telegram-llm-bot.db"
PROXY_FILE = "/etc/socks-monitor/.proxy_url"


def _resolve_proxy():
    val = os.environ.get("BOT_PROXY_URL", "")
    if val:
        return val
    try:
        return Path(PROXY_FILE).read_text().strip()
    except Exception:
        return ""


PROXY_URL = _resolve_proxy()

HEALTH_CHECK_PROMPT = "Ответь одним словом: столица Франции?"
HEALTH_CHECK_MAX_TOKENS = 10
HEALTH_CHECK_TIMEOUT = 30
WORKERS = 10
OPENROUTER_DELAY_SEC = 1.5

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
        "proxy": True,
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "models_url": "https://api.cerebras.ai/v1/models",
        "key_file": "/etc/socks-monitor/.cerebras_key",
        "supports_tools": False,
        "proxy": True,
    },
    "nvidia": {
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "key_file": "/etc/socks-monitor/.nvidia_key",
        "supports_tools": True,
        "proxy": False,
    },
    "huggingface": {
        "url": "https://router.huggingface.co/v1/chat/completions",
        "models_url": "https://router.huggingface.co/v1/models",
        "key_file": "/etc/socks-monitor/.hf_key",
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


def make_opener(use_proxy):
    if use_proxy:
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
    """Check a single model. Returns (model_id, latency_ms, available, supports_tools, category, error, rate_limited).

    rate_limited=True means the probe hit HTTP 429 and the model's `available` flag should be left as it was —
    a temporary key-quota exhaustion is not the same as the model being broken."""
    category = categorize_model(model_id)
    if category == "audio":
        return (model_id, 0, False, prov.get("supports_tools", False), "audio", None, False)
    if category not in ("text", "code"):
        return (model_id, 0, False, prov.get("supports_tools", False), category, None, False)
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
            return (model_id, latency, True, prov.get("supports_tools", False), category, None, False)
    except urllib.error.HTTPError as e:
        err = f"HTTP Error {e.code}: {e.reason}"
        rate_limited = (e.code == 429)
        log.info(f"  {'⏸' if rate_limited else '❌'} {model_id}: {err}")
        return (model_id, 0, False, False, category, err, rate_limited)
    except Exception as e:
        err = str(e)[:200]
        log.info(f"  ❌ {model_id}: {e}")
        return (model_id, 0, False, False, category, err, False)


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


def check_provider(conn, prov_name, openrouter_delay_sec=OPENROUTER_DELAY_SEC):
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
            if result[6]:  # rate_limited
                throttled += 1
            elif result[2]:  # available
                ok += 1
            elif result[4] in ("text", "code"):  # category
                fail += 1
            if i < len(models) - 1 and openrouter_delay_sec > 0:
                time.sleep(openrouter_delay_sec)

        # Phase 2: batch commit all results in a single short transaction.
        now = int(time.time())
        for model_id, latency, available, _, category, error, rate_limited in results:
            if category not in ("text", "code"):
                continue
            supports_tools = tools_map.get(model_id, prov.get("supports_tools", False))
            effective_available = _carry_or_set_availability(conn, prov_name, model_id, available, rate_limited)
            conn.execute(
                "INSERT OR REPLACE INTO model_health (provider, model_id, latency_ms, available, supports_tools, category, capabilities, last_check) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (prov_name, model_id, latency, effective_available,
                 1 if supports_tools else 0, category, capabilities_for_category(category), now))
            conn.execute(
                "INSERT INTO model_health_log (ts, provider, model_id, latency_ms, available, error) VALUES (?, ?, ?, ?, ?, ?)",
                (now, prov_name, model_id, latency, 1 if available else 0, error))
        conn.commit()
        log.info(f"{prov_name}: {ok} ok, {fail} failed, {throttled} rate-limited (kept prior state)")
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
            model_id, latency, available, supports_tools, category, error, rate_limited = result
            if rate_limited:
                throttled += 1
            elif available:
                ok += 1
            elif category in ("text", "code"):
                fail += 1

    # Phase 2: batch commit all results in a single short transaction.
    now = int(time.time())
    for model_id, latency, available, supports_tools, category, error, rate_limited in results:
        if category not in ("text", "code"):
            continue
        effective_available = _carry_or_set_availability(conn, prov_name, model_id, available, rate_limited)
        conn.execute(
            "INSERT OR REPLACE INTO model_health (provider, model_id, latency_ms, available, supports_tools, category, capabilities, last_check) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (prov_name, model_id, latency, effective_available,
             1 if supports_tools else 0, category, capabilities_for_category(category), now))
        conn.execute(
            "INSERT INTO model_health_log (ts, provider, model_id, latency_ms, available, error) VALUES (?, ?, ?, ?, ?, ?)",
            (now, prov_name, model_id, latency, 1 if available else 0, error))
    conn.commit()
    log.info(f"{prov_name}: {ok} ok, {fail} failed, {throttled} rate-limited (kept prior state)")


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
