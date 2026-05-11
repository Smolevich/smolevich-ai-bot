from __future__ import annotations

import os
from typing import Any

CONFIG = os.environ.get("BOT_CONFIG", "/etc/socks-monitor/config.json")
ADMIN_FILE = os.environ.get("BOT_ADMIN_FILE", "/etc/socks-monitor/.admin_id")
DB_FILE = os.environ.get("BOT_DB_FILE", "/var/lib/telegram-llm-bot.db")
SESSIONS_ROOT = os.environ.get("BOT_SESSIONS_ROOT", "/var/lib/vds-agent/sessions")

TUNNEL_URL = os.environ.get("BOT_TUNNEL_URL", "https://ai.smolevich.com")
MAX_CONTEXT_TOKENS = int(os.environ.get("BOT_MAX_CONTEXT_TOKENS", "64000"))
REQUIRED_CHANNEL = os.environ.get("BOT_REQUIRED_CHANNEL", "@naturalists_notes_st")
PROXY_URL = os.environ.get("BOT_PROXY_URL", "")

PROVIDERS: dict[str, dict[str, Any]] = {
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "models_url": "https://shir-man.com/api/free-llm/top-models",
        "key_env": "OPENROUTER_API_KEY",
        "key_file": "/etc/socks-monitor/.openrouter_key",
        "default_model": "inclusionai/ling-2.6-1t:free",
        "supports_tools": True,
        "proxy": False,
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models_url": "https://api.groq.com/openai/v1/models",
        "key_env": "GROQ_API_KEY",
        "key_file": "/etc/socks-monitor/.groq_key",
        "default_model": "llama-3.3-70b-versatile",
        "supports_tools": True,
        "proxy": False,
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "models_url": "https://api.cerebras.ai/v1/models",
        "key_env": "CEREBRAS_API_KEY",
        "key_file": "/etc/socks-monitor/.cerebras_key",
        "default_model": "llama-3.3-70b",
        "supports_tools": False,
        "proxy": False,
    },
    "nvidia": {
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "key_env": "NVIDIA_API_KEY",
        "key_file": "/etc/socks-monitor/.nvidia_key",
        "default_model": "meta/llama-3.1-70b-instruct",
        "supports_tools": True,
        "proxy": False,
    },
    "huggingface": {
        "url": "https://router.huggingface.co/v1/chat/completions",
        "models_url": "https://router.huggingface.co/v1/models",
        "key_env": "HF_TOKEN",
        "key_file": "/etc/socks-monitor/.hf_key",
        "default_model": "openai/gpt-oss-20b:fastest",
        "supports_tools": True,
        "proxy": False,
    },
}

PROVIDER_DEFAULT = "openrouter"
