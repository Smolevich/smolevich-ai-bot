#!/usr/bin/env python3
"""Image/video model checker (separate cron): discovers media models and updates model_health."""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

DB_FILE = "/var/lib/telegram-llm-bot.db"
PROXY_FILE = "/etc/socks-monitor/.proxy_url"

PROVIDERS = {
    "openrouter": {
        "models_url": "https://shir-man.com/api/free-llm/top-models",
        "key_file": "/etc/socks-monitor/.openrouter_key",
        "supports_tools": True,
        "proxy": False,
        "models_format": "top-models",
    },
    "groq": {
        "models_url": "https://api.groq.com/openai/v1/models",
        "key_file": "/etc/socks-monitor/.groq_key",
        "supports_tools": True,
        "proxy": True,
    },
    "cerebras": {
        "models_url": "https://api.cerebras.ai/v1/models",
        "key_file": "/etc/socks-monitor/.cerebras_key",
        "supports_tools": False,
        "proxy": True,
    },
    "nvidia": {
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "key_file": "/etc/socks-monitor/.nvidia_key",
        "supports_tools": True,
        "proxy": False,
    },
    "huggingface": {
        "models_url": "https://router.huggingface.co/v1/models",
        "key_file": "/etc/socks-monitor/.hf_key",
        "supports_tools": True,
        "proxy": False,
    },
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


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


def load_key(provider_name):
    try:
        return Path(PROVIDERS[provider_name]["key_file"]).read_text().strip()
    except Exception:
        return ""


def make_opener(use_proxy):
    if use_proxy and PROXY_URL:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": PROXY_URL, "http": PROXY_URL})
        )
    return urllib.request.build_opener()


def fetch_models(provider_name):
    prov = PROVIDERS[provider_name]
    api_key = load_key(provider_name)
    headers = {"User-Agent": "Mozilla/5.0"}
    if prov.get("models_format") != "top-models":
        if not api_key:
            return []
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(prov["models_url"], headers=headers)
    opener = make_opener(prov.get("proxy", False))
    with opener.open(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode())
    if prov.get("models_format") == "top-models":
        items = raw if isinstance(raw, list) else raw.get("models", raw.get("data", []))
    else:
        items = raw.get("data", [])
    out = []
    seen = set()
    for m in items:
        mid = m.get("id") if isinstance(m, dict) else m
        if mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def media_category(model_id):
    mid = (model_id or "").lower()
    if any(k in mid for k in ("image", "sdxl", "flux", "stable-diffusion", "visual")):
        return "image"
    if any(k in mid for k in ("video", "stream", "cosmos")):
        return "video"
    return ""


def check_provider(conn, provider_name):
    prov = PROVIDERS[provider_name]
    try:
        models = fetch_models(provider_name)
    except Exception as e:
        log.error(f"{provider_name}: fetch models failed: {e}")
        return

    now = int(time.time())
    media_models = [(m, media_category(m)) for m in models]
    media_models = [(m, c) for (m, c) in media_models if c in ("image", "video")]
    log.info(f"{provider_name}: found {len(media_models)} image/video models")

    for mid, category in media_models:
        conn.execute(
            "INSERT OR REPLACE INTO model_health (provider, model_id, latency_ms, available, supports_tools, category, capabilities, last_check) VALUES (?, ?, 0, 1, ?, ?, ?, ?)",
            (
                provider_name,
                mid,
                1 if prov.get("supports_tools", False) else 0,
                category,
                category,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO model_health_log (ts, provider, model_id, latency_ms, available, error) VALUES (?, ?, ?, 0, 1, NULL)",
            (now, provider_name, mid),
        )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Separate image/video model discovery checks")
    parser.add_argument("--provider", default="all", choices=["all", *PROVIDERS.keys()])
    args = parser.parse_args()
    providers = list(PROVIDERS.keys()) if args.provider == "all" else [args.provider]

    with sqlite3.connect(DB_FILE, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        for p in providers:
            check_provider(conn, p)


if __name__ == "__main__":
    main()
