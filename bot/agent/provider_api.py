from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

from agent.config import PROVIDERS, PROXY_URL


def load_provider_key(provider_name: str) -> str:
    prov = PROVIDERS.get(provider_name)
    if not prov:
        return ""
    env_key = os.environ.get(prov.get("key_env", ""), "").strip()
    if env_key:
        return env_key
    try:
        return Path(prov["key_file"]).read_text().strip()
    except Exception:
        return ""


def available_providers() -> list[str]:
    return [name for name in PROVIDERS if load_provider_key(name)]


def make_opener(use_proxy: bool):
    if use_proxy and PROXY_URL:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY_URL, "http": PROXY_URL}))
    return urllib.request.build_opener()


def fetch_models(provider_name: str, log: logging.Logger) -> list[dict[str, Any]]:
    prov = PROVIDERS.get(provider_name)
    if not prov:
        return []
    try:
        api_key = load_provider_key(provider_name)
        headers = {"User-Agent": "Mozilla/5.0"}
        if provider_name == "openrouter":
            req = urllib.request.Request(prov["models_url"], headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode()).get("models", [])
        headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(prov["models_url"], headers=headers)
        opener = make_opener(prov.get("proxy", False))
        with opener.open(req, timeout=10) as resp:
            data = json.loads(resp.read().decode()).get("data", [])
            return [{"id": m["id"], "supportsTools": prov["supports_tools"]} for m in data if m.get("id")]
    except Exception as e:
        log.error(f"fetch_models({provider_name}): {e}")
        return []

