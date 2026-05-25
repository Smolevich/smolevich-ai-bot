#!/usr/bin/env python3
"""Audio model checker (separate cron): probes STT/TTS endpoints and updates model_health."""
import argparse
import io
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

DB_FILE = "/var/lib/telegram-llm-bot.db"
PROXY_FILE = "/etc/socks-monitor/.proxy_url"
TIMEOUT_SEC = 45


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

PROVIDERS = {
    "groq": {
        "models_url": "https://api.groq.com/openai/v1/models",
        "base_url": "https://api.groq.com/openai/v1",
        "key_file": "/etc/socks-monitor/.groq_key",
        "proxy": USE_PROXY,
        "supports_tools": True,
    },
    "nvidia": {
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "key_file": "/etc/socks-monitor/.nvidia_key",
        "proxy": False,
        "supports_tools": True,
    },
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)


def load_key(provider_name):
    try:
        return Path(PROVIDERS[provider_name]["key_file"]).read_text().strip()
    except Exception:
        return ""


def make_opener(use_proxy):
    if use_proxy and PROXY_URL:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": PROXY_URL, "http": PROXY_URL}))
    return urllib.request.build_opener()


def fetch_models(provider_name, api_key):
    prov = PROVIDERS[provider_name]
    req = urllib.request.Request(
        prov["models_url"],
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "Mozilla/5.0"},
    )
    opener = make_opener(prov.get("proxy", False))
    with opener.open(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode())
    items = raw.get("data", [])
    out = []
    for x in items:
        mid = x.get("id", "")
        if mid:
            out.append(mid)
    return out


def _multipart_body(fields, files):
    boundary = "----AudioCheckBoundary" + str(int(time.time() * 1000))
    out = bytearray()
    for name, value in fields.items():
        out.extend(f"--{boundary}\r\n".encode())
        out.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.extend(str(value).encode())
        out.extend(b"\r\n")
    for f in files:
        out.extend(f"--{boundary}\r\n".encode())
        out.extend(f'Content-Disposition: form-data; name="{f["name"]}"; filename="{f["filename"]}"\r\n'.encode())
        out.extend(f'Content-Type: {f.get("content_type", "application/octet-stream")}\r\n\r\n'.encode())
        out.extend(f["content"])
        out.extend(b"\r\n")
    out.extend(f"--{boundary}--\r\n".encode())
    return boundary, bytes(out)


def _tiny_wav_bytes():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    return buf.getvalue()


def probe_stt(provider_name, prov, api_key, model_id):
    opener = make_opener(prov.get("proxy", False))
    fields = {"model": model_id, "response_format": "json", "language": "en", "temperature": "0"}
    files = [{"name": "file", "filename": "sample.wav", "content": _tiny_wav_bytes(), "content_type": "audio/wav"}]
    boundary, body = _multipart_body(fields, files)
    req = urllib.request.Request(
        f"{prov['base_url']}/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "User-Agent": "curl/8.7.1",
        },
    )
    t0 = time.time()
    with opener.open(req, timeout=TIMEOUT_SEC) as resp:
        json.loads(resp.read().decode())
    return int((time.time() - t0) * 1000)


def probe_tts(provider_name, prov, api_key, model_id):
    opener = make_opener(prov.get("proxy", False))
    payload = {"model": model_id, "input": "test", "voice": "autumn", "response_format": "wav"}
    req = urllib.request.Request(
        f"{prov['base_url']}/audio/speech",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "curl/8.7.1",
        },
    )
    t0 = time.time()
    with opener.open(req, timeout=TIMEOUT_SEC) as resp:
        _ = resp.read(64)
    return int((time.time() - t0) * 1000)


def classify_audio_capabilities(model_id):
    mid = (model_id or "").lower()
    caps = []
    if any(k in mid for k in ("whisper", "stt", "transcrib")):
        caps.append("audio:stt")
    if any(k in mid for k in ("orpheus", "tts", "speech")):
        caps.append("audio:tts")
    if not caps:
        caps.append("audio")
    return ",".join(caps)


def check_provider(conn, provider_name):
    prov = PROVIDERS[provider_name]
    api_key = load_key(provider_name)
    if not api_key:
        log.warning(f"{provider_name}: no API key")
        return
    try:
        models = fetch_models(provider_name, api_key)
    except Exception as e:
        log.error(f"{provider_name}: fetch models failed: {e}")
        return
    audio_models = [m for m in models if any(k in m.lower() for k in ("whisper", "stt", "transcrib", "orpheus", "tts", "speech"))]
    now = int(time.time())
    log.info(f"{provider_name}: probing {len(audio_models)} audio models")
    for mid in audio_models:
        capabilities = classify_audio_capabilities(mid)
        try:
            if any(k in mid.lower() for k in ("whisper", "stt", "transcrib")):
                latency = probe_stt(provider_name, prov, api_key, mid)
            else:
                latency = probe_tts(provider_name, prov, api_key, mid)
            conn.execute(
                "INSERT OR REPLACE INTO model_health (provider, model_id, latency_ms, available, supports_tools, category, capabilities, last_check) VALUES (?, ?, ?, 1, ?, 'audio', ?, ?)",
                (provider_name, mid, latency, 1 if prov.get("supports_tools", False) else 0, capabilities, now),
            )
            conn.execute(
                "INSERT INTO model_health_log (ts, provider, model_id, latency_ms, available, error) VALUES (?, ?, ?, ?, 1, NULL)",
                (now, provider_name, mid, latency),
            )
            log.info(f"  ✅ {mid}: {latency}ms")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace")
            except Exception:
                pass
            err = (body or f"HTTP {e.code}")[:300]
            conn.execute(
                "INSERT OR REPLACE INTO model_health (provider, model_id, latency_ms, available, supports_tools, category, capabilities, last_check) VALUES (?, ?, 0, 0, ?, 'audio', ?, ?)",
                (provider_name, mid, 1 if prov.get("supports_tools", False) else 0, capabilities, now),
            )
            conn.execute(
                "INSERT INTO model_health_log (ts, provider, model_id, latency_ms, available, error) VALUES (?, ?, ?, 0, 0, ?)",
                (now, provider_name, mid, err),
            )
            log.info(f"  ❌ {mid}: {err}")
        except Exception as e:
            err = str(e)[:300]
            conn.execute(
                "INSERT OR REPLACE INTO model_health (provider, model_id, latency_ms, available, supports_tools, category, capabilities, last_check) VALUES (?, ?, 0, 0, ?, 'audio', ?, ?)",
                (provider_name, mid, 1 if prov.get("supports_tools", False) else 0, capabilities, now),
            )
            conn.execute(
                "INSERT INTO model_health_log (ts, provider, model_id, latency_ms, available, error) VALUES (?, ?, ?, 0, 0, ?)",
                (now, provider_name, mid, err),
            )
            log.info(f"  ❌ {mid}: {err}")
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Separate audio model probes")
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
