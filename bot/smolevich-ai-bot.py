#!/usr/bin/env python3
"""Telegram bot with SQLite, Podman Sandboxing, Token Tracking, Feedback, Persistent Workspaces, and Debug Logging."""
import json
import logging
import urllib.request
import threading
import sys
import time
import os
import base64
import mimetypes
import shutil
import subprocess
import urllib.error
import shlex
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid
from agent.acpx_lock import acpx_lock, touch_active
from agent.config import (
    ADMIN_FILE,
    CONFIG,
    DB_FILE,
    MAX_CONTEXT_TOKENS,
    PROVIDERS,
    PROVIDER_DEFAULT,
    PROXY_URL,
    REQUIRED_CHANNEL,
    SESSIONS_ROOT,
    TUNNEL_URL,
)
from agent.text import (
    compact_messages_for_provider,
    estimate_tokens,
    sanitize_model_id,
)
from agent.entities import parse_markdown_to_entities
from agent.provider_api import available_providers, load_provider_key, make_opener
from agent.telegram_api import tg_get_file_bytes, tg_request, tg_send_document_bytes, tg_send_long_text, tg_send_text, multipart_body
from agent.db import DB

# Version stamp — CI replaces this placeholder before deploy; manual deploys keep "dev".
# Format: YYYY-MM-DD-<short_sha>
__VERSION__ = "dev"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

# Per-user in-flight guard: avoid parallel runs for the same chat user.
inflightUsers = set()
inflightUsersLock = threading.Lock()
inflightBusyNoticeTs = {}
INFLIGHT_BUSY_NOTICE_COOLDOWN_SEC = 8
pendingTextByUser = {}
pendingTextByUserLock = threading.Lock()
recentUpdateIds = {}
recentUpdateIdsLock = threading.Lock()
RECENT_UPDATE_TTL_SEC = 180
executorPool = ThreadPoolExecutor(max_workers=10)
runtimeStatus = {}
runtimeStatusLock = threading.Lock()
pendingSttUsers = set()
pendingSttUsersLock = threading.Lock()
pendingTtsUsers = set()
pendingTtsUsersLock = threading.Lock()
pendingVideoUsers = set()
pendingVideoUsersLock = threading.Lock()
pendingTranslateUsers = set()
pendingTranslateUsersLock = threading.Lock()
DEBUG_USERS = set()
DEBUG_USERS_LOCK = threading.Lock()
TELEGRAM_BOT_FILE_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024
STT_PROVIDER = "groq"
TTS_PROVIDER = "groq"
STT_DEFAULT_MODEL_BY_PROVIDER = {
    "groq": "whisper-large-v3-turbo",
}
TTS_DEFAULT_MODEL_BY_PROVIDER = {
    "groq": "playai-tts",
}
VIDEO_DETECTOR_PROVIDER = "nvidia"
VIDEO_DETECTOR_MODEL = "nvidia/ai-synthetic-video-detector"

# --- Tools ---
def tool_run_in_container(command, uid, allow_network=False):
    try:
        user_dir = os.path.join(SESSIONS_ROOT, str(uid))
        os.makedirs(user_dir, exist_ok=True)
        net_mode = "slirp4netns" if allow_network else "none"
        log.info(f"Podman (uid={uid}, net={net_mode}): {command}")
        cmd = ["podman", "run", "--rm", "--memory=128m", "--security-opt=no-new-privileges", f"--network={net_mode}", "-v", f"{user_dir}:/workspace:Z", "-w", "/workspace", "python:3.10-alpine", "sh", "-c", command]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return json.dumps({"stdout": res.stdout, "stderr": res.stderr, "exit_code": res.returncode}, ensure_ascii=False)
    except Exception as e:
        log.error(f"Podman error: {e}"); return f"Error: {e}"

def tool_get_weather(city):
    try:
        url = f"https://wttr.in/{urllib.request.quote(city)}?format=j1&lang=ru"
        with urllib.request.urlopen(url, timeout=10) as f:
            r = json.loads(f.read().decode())["current_condition"][0]
            return json.dumps({"temp": r["temp_C"], "desc": r.get("lang_ru", [{}])[0].get("value", "")}, ensure_ascii=False)
    except Exception as e: return "Weather unavailable"

def tool_get_exchange_rate(from_c, to_c, amount=1):
    try:
        with urllib.request.urlopen(f"https://open.er-api.com/v6/latest/{from_c.upper()}", timeout=10) as f:
            r = json.loads(f.read().decode()); rate = r["rates"].get(to_c.upper())
            return json.dumps({"rate": rate, "result": amount * rate}) if rate else "Unknown"
    except Exception as e: return "Rate unavailable"

TOOLS = [
    {"type": "function", "function": {"name": "execute_bash", "description": "Execute bash in Alpine Linux. Note: No 'requests' library, use urllib.request/wget/curl. Admin has internet.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "get_weather", "description": "Get weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}},
    {"type": "function", "function": {"name": "get_exchange_rate", "description": "Get rate", "parameters": {"type": "object", "properties": {"from_currency": {"type": "string"}, "to_currency": {"type": "string"}, "amount": {"type": "number", "default": 1}}, "required": ["from_currency", "to_currency"]}}}
]
TOOL_HANDLERS = {"get_weather": lambda a: tool_get_weather(a["city"]), "get_exchange_rate": lambda a: tool_get_exchange_rate(a["from_currency"], a["to_currency"], a.get("amount", 1))}

# --- Helpers ---

def load_bot_token():
    try:
        cfg = json.loads(Path(CONFIG).read_text())
        return Path(cfg["bot_token_file"]).read_text().strip()
    except Exception as e:
        log.error(f"Bot token error: {e}"); return ""

def load_admin():
    try: return int(Path(ADMIN_FILE).read_text().strip())
    except: return None

def categorize_model_local(model_id):
    mid = (model_id or "").lower()
    if any(k in mid for k in ("whisper", "speech", "voice", "tts", "riva-translate", "audio")):
        return "audio"
    if any(k in mid for k in ("image", "sdxl", "flux", "stable-diffusion", "visual")) and is_media_generation_model(mid):
        return "image"
    if any(k in mid for k in ("video", "stream", "cosmos")) and is_media_generation_model(mid):
        return "video"
    if any(k in mid for k in ("coder", "codestral", "devstral", "starcoder")):
        return "code"
    return "text"

def is_media_generation_model(model_id: str) -> bool:
    mid = (model_id or "").lower()
    reject = (
        "detector",
        "detection",
        "classifier",
        "classification",
        "moderation",
        "safety",
        "nsfw",
        "segment",
        "ocr",
        "recognition",
        "synthetic-video-detector",
    )
    return not any(k in mid for k in reject)

def is_video_detection_model(model_id: str) -> bool:
    mid = (model_id or "").lower()
    return any(k in mid for k in ("detector", "detection", "classifier", "classification", "synthetic-video-detector"))


def capabilities_for_model(provider, model_id):
    model = (model_id or "").lower()
    caps = []
    info = DB.get_model_info(provider, model_id)
    db_caps_raw = (info or {}).get("capabilities", "")
    if db_caps_raw:
        caps.extend([c.strip() for c in db_caps_raw.split(",") if c.strip()])
    category = (info or {}).get("category", "")
    if category in ("text", "code"):
        caps.append("text")
    if category in ("audio",) or any(k in model for k in ("whisper", "speech", "voice", "tts", "orpheus")):
        if any(k in model for k in ("whisper", "stt", "transcrib")):
            caps.append("audio:stt")
        if any(k in model for k in ("orpheus", "tts", "speech")):
            caps.append("audio:tts")
        if "audio:stt" not in caps and "audio:tts" not in caps:
            caps.append("audio")
    if (category == "image" or any(k in model for k in ("image", "sdxl", "flux", "stable-diffusion"))) and is_media_generation_model(model):
        caps.append("image")
    if (category == "video" or "video" in model) and is_media_generation_model(model):
        caps.append("video")
    if category == "video" and is_video_detection_model(model):
        caps.append("video:detect")
    if not caps:
        caps.append(categorize_model_local(model_id))
    # Deduplicate preserving order.
    seen = set()
    out = []
    for c in caps:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def ensure_text_model_for_session(sess):
    """Return (provider, model, switched) ensuring chat/code use text-capable model."""
    provider = sess.get("provider", PROVIDER_DEFAULT)
    model = sess.get("model", "")
    info = DB.get_model_info(provider, model) if model else None
    category = (info or {}).get("category", "")
    if category in ("", "text", "code"):
        return provider, model, False
    chosen = DB.pick_default_text_model(provider)
    if not chosen:
        provider = PROVIDER_DEFAULT
        chosen = DB.pick_default_text_model(provider) or PROVIDERS[provider]["default_model"]
    return provider, chosen, True

def has_stt_models():
    return bool(DB.pick_default_stt_model()[1])

def has_tts_models():
    return bool(DB.pick_default_tts_model()[1])

def pick_video_detector():
    prov, model = DB.pick_default_video_detector_model()
    if prov and model:
        return prov, model
    return None, None

def has_video_detector():
    return bool(pick_video_detector()[1])

def build_models_view(sess, category="text", limit=12):
    prov = sess["provider"]
    category = "text"
    db_category = "text"
    # For /models we must be deterministic and fast: use only DB cache, never provider network fetches.
    ms = DB.get_recent_models(prov, max_age_sec=1800, category=db_category, limit=max(limit * 3, 30))
    if not ms:
        ms = DB.get_healthy_models(prov, category=db_category, limit=max(limit * 3, 30))
    ms = ms[:limit]
    kb = [[{"text": "← Назад", "callback_data": "menu:settings"}]]
    for m in ms:
        mid = m["id"]
        latency = m.get("latency_ms") or 0
        available = m.get("available", True)
        tools_icon = "🛠" if m.get("supportsTools") else ""
        caps = capabilities_for_model(prov, mid)
        cap_icons = ""
        if "audio:stt" in caps:
            cap_icons += "🎙"
        if "audio:tts" in caps:
            cap_icons += "🔊"
        if "image" in caps:
            cap_icons += "🖼"
        if "video" in caps:
            cap_icons += "🎬"
        if "video:detect" in caps:
            cap_icons += "🕵️"
        if available and latency:
            status_icon = "🟢"
        elif available:
            status_icon = "⚪"
        else:
            status_icon = "🔴"
        latency_tag = f" {latency}ms" if latency else ""
        label = mid.split("/")[-1] if "/" in mid else mid
        kb.append([{"text": f"{status_icon}{tools_icon}{cap_icons} {label}{latency_tag}", "callback_data": f"set_model:{mid}"}])
    txt = f"Текстовые модели ({prov}):"
    return txt, kb

def build_menu_root(sess, is_admin=False):
    """Корневое inline-меню /menu."""
    ui_lang = sess.get("ui_lang", "ru")
    is_en = ui_lang == "en"
    kb = [
        [{"text": ("💬 Chat" if is_en else "💬 Чат"), "callback_data": "menu:chat"},
         {"text": ("🤖 Model" if is_en else "🤖 Модель"), "callback_data": "menu:model"}],
    ]
    voice_row = []
    if has_stt_models():
        voice_row.append({"text": ("🎙 STT" if is_en else "🎙 Речь в текст"), "callback_data": "menu:stt"})
    if has_tts_models():
        voice_row.append({"text": ("🔊 TTS" if is_en else "🔊 Текст в речь"), "callback_data": "menu:tts"})
    if voice_row:
        kb.append(voice_row)
    if has_video_detector():
        kb.append([{"text": "🕵️ VideoDetect", "callback_data": "menu:video"}])
    if is_admin:
        kb.append([{"text": "🛠 Admin", "callback_data": "menu:admin"}])
    kb.append(
        [{"text": ("⚙️ Settings" if is_en else "⚙️ Настройки"), "callback_data": "menu:settings"}],
    )
    txt = (
        "🤖 What do you want to do?\n\n"
        "💬 Chat — send text, I reply.\n"
        "🤖 Model — choose text model.\n"
        "🎙/🔊 Voice modes appear only when available."
    ) if is_en else (
        "🤖 Что хочешь сделать?\n\n"
        "💬 Чат — пиши текст, отвечу.\n"
        "🤖 Модель — выбрать текстовую модель.\n"
        "🎙/🔊 Голосовые режимы показываются только когда доступны."
    )
    return txt, kb

def build_menu_settings(sess, is_admin=False):
    """Сабменю настроек."""
    is_en = sess.get("ui_lang", "ru") == "en"
    model = sess.get("model", "")
    prov = sess.get("provider", PROVIDER_DEFAULT)
    model_short = model.split("/")[-1] if "/" in model else model
    if len(model_short) > 30:
        model_short = model_short[:27] + "…"
    ui_lang = sess.get("ui_lang", "ru")
    lang_label = "RU" if ui_lang == "ru" else "EN"
    kb = [
        [{"text": (f"🤖 Model: {model_short}" if is_en else f"🤖 Модель: {model_short}"), "callback_data": "menu:model"}],
        [{"text": (f"🔌 Provider: {prov}" if is_en else f"🔌 Провайдер: {prov}"), "callback_data": "menu:provider"}],
        [{"text": (f"🌐 Language: {lang_label}" if is_en else f"🌐 Язык: {lang_label}"), "callback_data": "menu:lang_toggle"}],
    ]
    kb += [
        [{"text": ("🔄 Reset history" if is_en else "🔄 Сброс истории"), "callback_data": "menu:reset"}],
        [{"text": ("❓ Help" if is_en else "❓ Помощь"), "callback_data": "menu:help"}],
        [{"text": ("← Back" if is_en else "← Назад"), "callback_data": "menu:back"}],
    ]
    return ("⚙️ Settings" if is_en else "⚙️ Настройки"), kb

def build_admin_menu(sess):
    mode = sess.get("engine_mode", "native")
    tools_on = sess.get("tools_enabled", True)
    kb = [
        [{"text": "🛠 Code mode", "callback_data": "menu:code"}],
        [{"text": f"⚙️ Engine: {mode}", "callback_data": "menu:mode"}],
        [{"text": f"🧰 Tools: {'on' if tools_on else 'off'}", "callback_data": "menu:tools"}],
        [{"text": "📈 Status", "callback_data": "menu:status"}],
        [{"text": "🏆 Top models", "callback_data": "menu:top"}],
        [{"text": "👥 Users", "callback_data": "menu:users"}],
        [{"text": "🐛 Debug", "callback_data": "menu:debug"}],
        [{"text": "← Назад", "callback_data": "menu:back"}],
    ]
    return "🛠 Admin", kb

def groq_transcribe_audio(audio_bytes, filename="audio.ogg", language="ru", model="whisper-large-v3-turbo"):
    api_key = load_provider_key(STT_PROVIDER)
    if not api_key:
        raise RuntimeError("No GROQ API key configured")
    fields = {
        "model": model,
        "response_format": "json",
        "language": language,
        "temperature": "0",
    }
    fn = filename or "audio.ogg"
    ext = fn.lower().rsplit(".", 1)[-1] if "." in fn else ""
    # Telegram voice files are often .oga; Groq validates extension against a fixed allowlist.
    if ext == "oga":
        fn = fn[: -(len(ext))] + "ogg"
        ext = "ogg"
    if not ext:
        fn = f"{fn}.ogg"
        ext = "ogg"
    content_type = "application/octet-stream"
    if ext in ("ogg", "oga"):
        content_type = "audio/ogg"
    elif ext in ("mp3",):
        content_type = "audio/mpeg"
    elif ext in ("wav",):
        content_type = "audio/wav"
    elif ext in ("m4a",):
        content_type = "audio/mp4"
    files = [{"name": "file", "filename": fn, "content": audio_bytes, "content_type": content_type}]
    boundary, body = multipart_body(fields, files)
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "User-Agent": "curl/8.7.1",
        },
    )
    opener = make_opener(True)
    try:
        with opener.open(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {e.code}: {(body or e.reason)[:400]}")
    return (data.get("text") or "").strip()

def groq_tts(text, model="canopylabs/orpheus-v1-english", voice="autumn"):
    api_key = load_provider_key(TTS_PROVIDER)
    if not api_key:
        raise RuntimeError("No GROQ API key configured")
    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "wav",
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/speech",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "curl/8.7.1",
        },
    )
    opener = make_opener(True)
    try:
        with opener.open(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {e.code}: {(body or e.reason)[:400]}")


def transcribe_audio_for_provider(provider, audio_bytes, filename, model, language="ru"):
    prov = PROVIDERS.get(provider, {})
    api_key = load_provider_key(provider)
    if not api_key:
        raise RuntimeError(f"No API key configured for provider {provider}")
    base_url = (prov.get("url") or "").rsplit("/chat/completions", 1)[0]
    if not base_url:
        raise RuntimeError(f"Provider {provider} has no OpenAI-compatible base URL")
    target_model = model or STT_DEFAULT_MODEL_BY_PROVIDER.get(provider, "")
    if not target_model:
        raise RuntimeError(f"No STT model configured for provider {provider}")
    fields = {
        "model": target_model,
        "response_format": "json",
        "language": language,
        "temperature": "0",
    }
    fn = filename or "audio.ogg"
    ext = fn.lower().rsplit(".", 1)[-1] if "." in fn else ""
    if ext == "oga":
        fn = fn[: -(len(ext))] + "ogg"
        ext = "ogg"
    if not ext:
        fn = f"{fn}.ogg"
        ext = "ogg"
    content_type = "application/octet-stream"
    if ext in ("ogg", "oga"):
        content_type = "audio/ogg"
    elif ext in ("mp3",):
        content_type = "audio/mpeg"
    elif ext in ("wav",):
        content_type = "audio/wav"
    elif ext in ("m4a",):
        content_type = "audio/mp4"
    files = [{"name": "file", "filename": fn, "content": audio_bytes, "content_type": content_type}]
    boundary, body = multipart_body(fields, files)
    req = urllib.request.Request(
        f"{base_url}/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "User-Agent": "curl/8.7.1",
        },
    )
    opener = make_opener(prov.get("proxy", False))
    with opener.open(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    return (data.get("text") or "").strip(), provider, target_model


def tts_for_provider(provider, text, model):
    prov = PROVIDERS.get(provider, {})
    api_key = load_provider_key(provider)
    if not api_key:
        raise RuntimeError(f"No API key configured for provider {provider}")
    base_url = (prov.get("url") or "").rsplit("/chat/completions", 1)[0]
    if not base_url:
        raise RuntimeError(f"Provider {provider} has no OpenAI-compatible base URL")
    target_model = model or TTS_DEFAULT_MODEL_BY_PROVIDER.get(provider, "")
    if not target_model:
        raise RuntimeError(f"No TTS model configured for provider {provider}")
    payload = {
        "model": target_model,
        "input": text,
        "voice": "autumn",
        "response_format": "wav",
    }
    req = urllib.request.Request(
        f"{base_url}/audio/speech",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "curl/8.7.1",
        },
    )
    opener = make_opener(prov.get("proxy", False))
    with opener.open(req, timeout=120) as resp:
        return resp.read(), provider, target_model


def transcribe_audio_with_fallback(selected_provider, audio_bytes, filename, model):
    errors = []
    for provider in [selected_provider, STT_PROVIDER]:
        if provider in (None, ""):
            continue
        try:
            return transcribe_audio_for_provider(provider, audio_bytes, filename, model)
        except Exception as e:
            errors.append(f"{provider}: {e}")
    raise RuntimeError("; ".join(errors)[:400])


def tts_with_fallback(selected_provider, text, model):
    errors = []
    for provider in [selected_provider, TTS_PROVIDER]:
        if provider in (None, ""):
            continue
        try:
            return tts_for_provider(provider, text, model)
        except Exception as e:
            errors.append(f"{provider}: {e}")
    raise RuntimeError("; ".join(errors)[:400])

NVIDIA_MAXINE_FUNCTIONS = {
    "nvidia/ai-synthetic-video-detector": "847b6e53-0133-452d-ab85-d7acf3ace723",
}
NVIDIA_MAXINE_IMAGE = "nvidia-maxine-svd"
NVIDIA_MAXINE_TARGET = "grpc.nvcf.nvidia.com:443"


def analyze_video_detection(api_url, api_key, model, video_bytes, filename="video.mp4", use_proxy=False):
    function_id = NVIDIA_MAXINE_FUNCTIONS.get(model)
    if not function_id:
        raise RuntimeError(f"No NVCF function mapping for model {model}")
    import subprocess, tempfile, re
    with tempfile.TemporaryDirectory(prefix="svd-") as tmp:
        os.chmod(tmp, 0o777)
        in_path = os.path.join(tmp, "in.mp4")
        out_path = os.path.join(tmp, "out.csv")
        with open(in_path, "wb") as f:
            f.write(video_bytes or b"")
        cmd = [
            "podman", "run", "--rm",
            "-v", f"{tmp}:/data:Z",
            NVIDIA_MAXINE_IMAGE,
            "--function-id", function_id,
            "--api-key", api_key,
            "--video-input", "/data/in.mp4",
            "--save-csv", "/data/out.csv",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        stdout = proc.stdout or ""
        if proc.returncode != 0:
            tail = (proc.stderr or stdout).strip().splitlines()[-3:]
            raise RuntimeError("podman: " + " | ".join(tail))
        verdict = re.search(r"VERDICT:\s*(\w+)\s*\(confidence:\s*([\d.]+)%\)", stdout)
        prob = re.search(r"Final probability:\s*([\d.]+)", stdout)
        frames = re.search(r"Total frames processed:\s*(\d+)", stdout)
        if verdict:
            label = verdict.group(1).capitalize()
            conf = verdict.group(2)
            lines = [f"*{label}* (confidence {conf}%)"]
            if prob: lines.append(f"P(synthetic) = {prob.group(1)}")
            if frames: lines.append(f"frames analyzed: {frames.group(1)}")
            return "\n".join(lines)
        return stdout.strip()[-800:] or "no output"


BOT_COMMANDS = [
    {"command": "menu", "description": "Меню (главное)"},
    {"command": "help", "description": "Помощь"},
]

def set_user_commands(token, uid):
    """Keep every chat on the same minimal command list; actions live in buttons."""
    payload = {"commands": BOT_COMMANDS, "scope": {"type": "chat", "chat_id": uid}}
    res = tg_request(token, "setMyCommands", payload)
    if not res.get("ok"):
        log.warning(f"setMyCommands per-chat failed for uid={uid}: {res}")


def format_video_analysis(raw_analysis, lang="ru", caption_text=""):
    text = (raw_analysis or "").strip()
    verdict_match = re.search(r"\*(Synthetic|Real)\*\s*\(confidence\s*([\d.]+)%\)", text, re.IGNORECASE)
    prob_match = re.search(r"P\(synthetic\)\s*=\s*([\d.]+)", text, re.IGNORECASE)
    frames_match = re.search(r"frames analyzed:\s*(\d+)", text, re.IGNORECASE)
    if not verdict_match:
        return text

    verdict_raw = verdict_match.group(1).lower()
    confidence_pct = float(verdict_match.group(2))
    synthetic_prob = float(prob_match.group(1)) if prob_match else (confidence_pct / 100.0 if verdict_raw == "synthetic" else max(0.0, 1.0 - (confidence_pct / 100.0)))
    frames_count = int(frames_match.group(1)) if frames_match else 0

    if lang == "en":
        verdict_text = "likely AI-generated/synthetic video" if verdict_raw == "synthetic" else "likely real video"
        risk_line = "High likelihood of synthetic content." if synthetic_prob >= 0.85 else ("Medium likelihood of synthetic content." if synthetic_prob >= 0.60 else "Low likelihood of synthetic content.")
        lines = [
            f"🕵️ Video analysis result: {verdict_text}",
            f"Model confidence: {confidence_pct:.2f}%",
            f"Estimated synthetic probability: {synthetic_prob * 100:.2f}%",
            f"Frames analyzed: {frames_count}",
            risk_line,
            "Note: this is a probabilistic detector output, not absolute proof.",
        ]
        if caption_text:
            lines.insert(0, f"Context: {caption_text}")
        return "\n".join(lines)

    verdict_text = "вероятно синтетическое (AI) видео" if verdict_raw == "synthetic" else "вероятно реальное видео"
    risk_line = "Высокая вероятность синтетики." if synthetic_prob >= 0.85 else ("Средняя вероятность синтетики." if synthetic_prob >= 0.60 else "Низкая вероятность синтетики.")
    lines = [
        f"🕵️ Результат анализа видео: {verdict_text}",
        f"Уверенность модели: {confidence_pct:.2f}%",
        f"Оценка вероятности синтетики: {synthetic_prob * 100:.2f}%",
        f"Кадров проанализировано: {frames_count}",
        risk_line,
        "Важно: это вероятностная оценка модели, а не абсолютное доказательство.",
    ]
    if caption_text:
        lines.insert(0, f"📝 Контекст: {caption_text}")
    return "\n".join(lines)

def set_bot_commands(token):
    commands = BOT_COMMANDS

    # Telegram command menu may be scoped and language-specific.
    # Publish commands for default + private chats, both generic and RU locale.
    variants = [
        {"scope": {"type": "default"}},
        {"scope": {"type": "all_private_chats"}},
        {"scope": {"type": "default"}, "language_code": "ru"},
        {"scope": {"type": "all_private_chats"}, "language_code": "ru"},
    ]

    for v in variants:
        payload = {
            "commands": commands,
            "scope": v["scope"],
        }
        if "language_code" in v:
            payload["language_code"] = v["language_code"]
        res = tg_request(token, "setMyCommands", payload)
        if not res.get("ok"):
            log.warning(
                "setMyCommands failed for "
                f"scope={v['scope']} lang='{v.get('language_code', '-')}'"
                f": {res}"
            )

    menu_scopes = [
        {"type": "default"},
        {"type": "all_private_chats"},
    ]
    for scope in menu_scopes:
        res = tg_request(
            token,
            "setChatMenuButton",
            {"scope": scope, "menu_button": {"type": "commands"}},
        )
        if not res.get("ok"):
            log.warning(f"setChatMenuButton failed for scope={scope}: {res}")

def is_subscribed(token, user_id):
    try:
        res = tg_request(token, "getChatMember", {"chat_id": REQUIRED_CHANNEL, "user_id": user_id})
        return res.get("ok") and res["result"].get("status") in ["creator", "administrator", "member"]
    except: return False

# --- Logic ---
def format_wait_time(seconds):
    if seconds < 60: return f"{int(seconds)}s"
    if seconds < 3600: return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"

def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def extract_rate_limit_headers(headers):
    if not headers:
        return {}
    out = {}
    for k, v in headers.items():
        lk = k.lower()
        if ("ratelimit" in lk) or ("rate-limit" in lk) or ("retry-after" in lk):
            out[lk] = str(v)
    return out


def fetch_openrouter_key_limits(api_key: str) -> dict:
    if not api_key:
        return {}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/key",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "curl/8.7.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read().decode())
        data = raw.get("data", raw)
        out = {}
        for k in (
            "limit",
            "limit_remaining",
            "usage",
            "is_free_tier",
            "rate_limit",
            "credits",
            "credits_remaining",
        ):
            if k in data:
                out[k] = data[k]
        return out
    except Exception:
        return {}

def ask_llm(api_url, api_key, model, messages, uid=None, admin_id=None, use_tools=True, use_proxy=False):
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    meta = {"finish_reason": None, "tool_calls_total": 0, "error": None, "http_latency_ms": 0, "rate_limits": {}}
    roles = [m.get("role", "?") for m in messages]
    log.info(f"ask_llm: model={model} tools={use_tools} proxy={use_proxy} msgs={len(messages)} roles={roles} est_tokens={estimate_tokens(messages)}")
    opener = make_opener(use_proxy)
    req_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        # Cloudflare edge on some providers/proxy paths can block default urllib UA (error 1010).
        "User-Agent": "curl/8.7.1",
    }
    retry_use_tools = use_tools
    for attempt in range(10):
        payload = {"model": model, "messages": messages, "max_tokens": 4096}
        if retry_use_tools: payload.update({"tools": TOOLS, "tool_choice": "auto"})
        req = urllib.request.Request(api_url, json.dumps(payload).encode(), req_headers)
        try:
            t_http = time.time()
            with opener.open(req, timeout=120) as f:
                meta["http_latency_ms"] += int((time.time() - t_http) * 1000)
                meta["rate_limits"] = extract_rate_limit_headers(f.headers)
                res = json.loads(f.read().decode())
                u = res.get("usage", {}); usage["prompt_tokens"] += u.get("prompt_tokens", 0); usage["completion_tokens"] += u.get("completion_tokens", 0)
                msg = res["choices"][0]["message"]
                finish = res["choices"][0].get("finish_reason", "?")
                meta["finish_reason"] = finish
                if not msg.get("tool_calls"):
                    content = (msg.get("content") or "").strip()
                    log.info(f"ask_llm response: finish={finish} content_len={len(content)} tool_calls=0")
                    if content:
                        # If the model was cut off by max_tokens, request continuation.
                        if finish == "length":
                            messages.append(msg)
                            messages.append({"role": "user", "content": "Continue exactly where you left off."})
                            full_content = content
                            for contIdx in range(3):
                                cont_payload = {"model": model, "messages": messages, "max_tokens": 4096}
                                cont_req = urllib.request.Request(api_url, json.dumps(cont_payload).encode(), req_headers)
                                try:
                                    t_cont = time.time()
                                    with opener.open(cont_req, timeout=120) as cf:
                                        meta["http_latency_ms"] += int((time.time() - t_cont) * 1000)
                                        cont_res = json.loads(cf.read().decode())
                                        cu = cont_res.get("usage", {})
                                        usage["prompt_tokens"] += cu.get("prompt_tokens", 0)
                                        usage["completion_tokens"] += cu.get("completion_tokens", 0)
                                        cont_msg = cont_res["choices"][0]["message"]
                                        cont_finish = cont_res["choices"][0].get("finish_reason", "?")
                                        cont_text = (cont_msg.get("content") or "").strip()
                                        if cont_text:
                                            full_content += "\n" + cont_text
                                        messages.append(cont_msg)
                                        if cont_finish != "length":
                                            break
                                        messages.append({"role": "user", "content": "Continue exactly where you left off."})
                                except Exception as e:
                                    log.warning(f"Continuation request failed: {e}")
                                    break
                            return full_content, usage, meta
                        return content, usage, meta
                    log.warning(f"Empty model response. model={model} finish={finish} raw_keys={list(msg.keys())}")
                    meta["error"] = "empty_response"
                    return "No response", usage, meta
                meta["tool_calls_total"] += len(msg["tool_calls"])
                log.info(f"ask_llm response: finish={finish} tool_calls={len(msg['tool_calls'])} funcs={[tc['function']['name'] for tc in msg['tool_calls']]}")
                messages.append(msg)
                for tc in msg["tool_calls"]:
                    fname = tc["function"]["name"]
                    raw_args = tc["function"].get("arguments", "") or ""
                    try:
                        fargs = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError as e:
                        log.warning(f"Invalid tool args JSON from model={model} func={fname}: {e}. raw={raw_args[:500]}")
                        # Keep the loop alive: send a structured tool-side error back to the model.
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(
                                {
                                    "error": "invalid_tool_arguments_json",
                                    "function": fname,
                                    "details": str(e),
                                },
                                ensure_ascii=False,
                            ),
                        })
                        continue
                    if fname == "execute_bash":
                        cmd = fargs.get("command") or next(iter(fargs.values()), "echo 'no command'")
                        res_t = tool_run_in_container(cmd, uid=uid, allow_network=(uid == admin_id))
                        try:
                            check = json.loads(res_t)
                            if not check.get("stdout") and not check.get("stderr") and check.get("exit_code") == 0:
                                res_t = "Command executed successfully but returned no output."
                        except: pass
                    else:
                        res_t = TOOL_HANDLERS.get(fname)(fargs) if TOOL_HANDLERS.get(fname) else "Error"
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": res_t})
        except urllib.error.HTTPError as e:
            try:
                meta["http_latency_ms"] += int((time.time() - t_http) * 1000)
            except Exception:
                pass
            meta["error"] = f"HTTP {e.code}"
            meta["rate_limits"] = extract_rate_limit_headers(e.headers)
            err_body = ""
            try:
                err_body = e.read().decode(errors="replace")
            except Exception:
                err_body = ""
            hint = "\nОткрой /menu или /models чтобы выбрать другую."
            if e.code == 404: return f"❌ Модель `{model}` недоступна.{hint}", usage, meta
            if e.code == 429:
                val = e.headers.get("Retry-After") or e.headers.get("x-ratelimit-reset")
                try:
                    v = float(val); v = v/1000 if v > 1e11 else v
                    if v > 1e9: v -= time.time()
                    wait_info = f" (Retry in {format_wait_time(max(0, v))})"
                except: wait_info = f" ({val})"
                return f"❌ Лимит запросов{wait_info}.{hint}", usage, meta
            if err_body:
                log.warning(f"HTTP {e.code} from provider for model={model}: {err_body[:400]}")
            return f"❌ Ошибка провайдера (HTTP {e.code}).{hint}", usage, meta
        except Exception as e:
            try:
                meta["http_latency_ms"] += int((time.time() - t_http) * 1000)
            except Exception:
                pass
            meta["error"] = str(e)[:200]
            log.error(f"ask_llm error: {e}"); return f"❌ Error: {e}", usage, meta
    meta["error"] = "loop_limit"
    return "❌ Agent loop limit reached.", usage, meta

def compact_history(api_url, api_key, model, history, uid, admin_id, use_proxy=False):
    to_sum = history[:-4]; keep = history[-4:]
    p = [{"role": "system", "content": "Summarize concisely."}] + to_sum
    sum_text, _, _ = ask_llm(api_url, api_key, model, p, uid=uid, admin_id=admin_id, use_tools=False, use_proxy=use_proxy)
    return [{"role": "system", "content": f"Summary: {sum_text}"}] + keep

def ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass

def acp_agent_for_mode(mode):
    m = (mode or "").strip().lower()
    if m in ("claude", "opencode", "pi"):
        return m
    return "claude"

def ask_via_acpx(uid, text, sess):
    try:
        mode_model = sanitize_model_id(sess.get("model") or "")
        mode = sess.get("engine_mode", "native")
        agent = acp_agent_for_mode(mode)
        # Each request gets its own isolated workspace subdirectory.
        user_dir = os.path.join(SESSIONS_ROOT, str(uid))
        ensure_dir(user_dir)
        session_uuid = str(uuid.uuid4())
        DB.set_last_session_id(uid, session_uuid)
        run_id = f"{int(time.time())}_{session_uuid[:8]}"
        with runtimeStatusLock:
            st = runtimeStatus.get(uid, {})
            st.update({
                "active": True,
                "active_session_id": session_uuid,
                "last_session_id": session_uuid,
                "last_mode": sess.get("engine_mode", "native"),
                "last_provider": sess.get("provider", PROVIDER_DEFAULT),
                "last_model": mode_model,
                "last_start_ts": int(time.time()),
            })
            runtimeStatus[uid] = st
        cwd = os.path.join(user_dir, run_id)
        ensure_dir(cwd)
        try:
            os.chmod(cwd, 0o777)
        except Exception:
            pass

        env = os.environ.copy()
        provider = sess.get("provider", "openrouter")
        prov_cfg = PROVIDERS.get(provider, PROVIDERS[PROVIDER_DEFAULT])
        try:
            api_key = load_provider_key(provider) or load_provider_key(PROVIDER_DEFAULT)
            # Base URL: strip /chat/completions to get the base
            base_url = prov_cfg["url"].rsplit("/chat/completions", 1)[0]
            env["OPENAI_API_KEY"] = api_key
            env["OPENAI_BASE_URL"] = base_url
            env["ANTHROPIC_AUTH_TOKEN"] = api_key
            # Verified empirically against claude-code 2.1.138 + claude-agent-acp 0.33.1:
            # empty ANTHROPIC_API_KEY makes claude-code skip the auth header and
            # OpenRouter responds with 403. Matches test-claude-openrouter.sh.
            # Do not "fix" back to "" based on stale OpenRouter docs.
            env["ANTHROPIC_API_KEY"] = api_key
            env["ANTHROPIC_BASE_URL"] = base_url.replace("/v1", "")
        except Exception:
            pass
        env["OPENAI_MODEL"] = mode_model
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = mode_model
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = mode_model
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = mode_model
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = mode_model
        env["ACPX_APPEND_SYSTEM_PROMPT"] = "Be concise. Execute directly. Return only essential output and short conclusions."
        # Force non-interactive permission behavior in claude-agent-acp settings.
        claude_cfg_dir = os.path.join(cwd, ".claude")
        try:
            os.makedirs(claude_cfg_dir, exist_ok=True)
            # Container runs as uid 1000; claude-code writes state into CLAUDE_CONFIG_DIR.
            os.chmod(claude_cfg_dir, 0o777)
            settings_path = os.path.join(claude_cfg_dir, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"permissions": {"defaultMode": "bypassPermissions"}}, f)
        except Exception as e:
            log.warning(f"Failed to write claude settings in {claude_cfg_dir}: {e}")
        env["CLAUDE_CONFIG_DIR"] = claude_cfg_dir
        # claude-agent-acp disallows bypassPermissions for root unless IS_SANDBOX is set.
        env["IS_SANDBOX"] = "1"
        # Don't put HTTP(S)_PROXY into the subprocess env unconditionally:
        # podman auto-forwards those host vars into the container, which routed
        # OpenRouter traffic through an unrelated proxy and produced 403s for
        # providers with use_proxy=False. Proxy is wired explicitly via `-e`
        # below only when prov_cfg["proxy"] is True.

        # Prepare workspace subdirs for containerized harness.
        for sub in [".claude-home", ".claude-config", ".claude-cache", ".claude-state"]:
            sub_path = os.path.join(cwd, sub)
            try:
                os.makedirs(sub_path, exist_ok=True)
                os.chmod(sub_path, 0o777)
            except Exception:
                pass

        # All harness modes run inside the podman container.
        acpx_timeout = "135"
        use_proxy = prov_cfg.get("proxy", False)
        podman_base = [
            "podman", "run", "--rm", "--network=host", "--user", "1000:1000",
            "-e", f"OPENAI_API_KEY={env.get('OPENAI_API_KEY', '')}",
            "-e", f"ANTHROPIC_BASE_URL={env.get('ANTHROPIC_BASE_URL', '')}",
            "-e", f"ANTHROPIC_AUTH_TOKEN={env.get('ANTHROPIC_AUTH_TOKEN', '')}",
            "-e", f"ANTHROPIC_API_KEY={env.get('ANTHROPIC_API_KEY', '')}",
            "-e", f"OPENAI_BASE_URL={env.get('OPENAI_BASE_URL', '')}",
            "-e", f"OPENAI_MODEL={mode_model}",
            "-e", f"ANTHROPIC_DEFAULT_OPUS_MODEL={mode_model}",
            "-e", f"ANTHROPIC_DEFAULT_SONNET_MODEL={mode_model}",
            "-e", f"ANTHROPIC_DEFAULT_HAIKU_MODEL={mode_model}",
            "-e", f"CLAUDE_CODE_SUBAGENT_MODEL={mode_model}",
            "-e", "HOME=/workspace/.claude-home",
            "-e", "XDG_CONFIG_HOME=/workspace/.claude-config",
            "-e", "XDG_CACHE_HOME=/workspace/.claude-cache",
            "-e", "CLAUDE_CONFIG_DIR=/workspace/.claude",
            "-e", "IS_SANDBOX=1",
            "-e", f"ACPX_APPEND_SYSTEM_PROMPT={env.get('ACPX_APPEND_SYSTEM_PROMPT', '')}",
        ]
        if use_proxy:
            podman_base += [
                "-e", f"HTTPS_PROXY={PROXY_URL}",
                "-e", f"HTTP_PROXY={PROXY_URL}",
                "-e", f"ALL_PROXY={PROXY_URL}",
            ]
        # pi and opencode go through OpenAI-compat path and want the active
        # provider's native env var (claude doesn't need this — it reads
        # ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY already in podman_base).
        if agent in ("pi", "opencode"):
            nativeEnvMap = {
                "openrouter": "OPENROUTER_API_KEY",
                "groq": "GROQ_API_KEY",
                "cerebras": "CEREBRAS_API_KEY",
            }
            nativeEnv = nativeEnvMap.get(provider)
            nativeKey = env.get("OPENAI_API_KEY", "")
            if nativeEnv and nativeKey:
                podman_base += ["-e", f"{nativeEnv}={nativeKey}"]
        podman_base += [
            "-v", f"{cwd}:/workspace",
            "-w", "/workspace",
            "localhost/acpx-claude:latest",
        ]
        if agent == "claude":
            # Route Claude mode through acpx wrapper for consistent model/env handling.
            run_cmd = podman_base + [
                "acpx", "--cwd", "/workspace", "--format", "text",
                "--approve-all", "--non-interactive-permissions", "deny",
                "--timeout", acpx_timeout,
                "claude", "exec", text,
            ]
        elif agent == "pi":
            # Direct invocation — pi-acp wrapper inside acpx pulls a different
            # package via npx at runtime and fails auth. Native pi binary in
            # the image accepts --provider with the native env var set above.
            # Pi's default coding prompt/context is large enough to trip
            # free-tier TPM limits, so keep the one-shot harness lean.
            pi_cmd = [
                "pi", "-p", "--no-session",
                "--model", mode_model,
                "--provider", provider,
                "--system-prompt", "You are a concise sandboxed coding assistant. Use tools only when needed. Return essential output.",
                "--no-context-files",
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                "--no-themes",
                text,
            ]
            run_cmd = podman_base + pi_cmd
        elif agent == "opencode":
            # `opencode run` needs an interactive auth setup that doesn't apply
            # in our ephemeral cwd. Route through opencode's ACP server via the
            # acpx --agent escape hatch — opencode picks up the provider from
            # the env vars below and skips its own credentials store.
            run_cmd = podman_base + [
                "acpx", "--agent", "opencode acp",
                "--cwd", "/workspace", "--format", "text",
                "--approve-all", "--non-interactive-permissions", "deny",
                "--timeout", acpx_timeout,
                "exec", text,
            ]
        else:
            run_cmd = podman_base + [
                "acpx", "--cwd", "/workspace", "--format", "text",
                "--approve-all", "--non-interactive-permissions", "deny",
                "--timeout", acpx_timeout,
                agent, "exec", text,
            ]
        log.info(f"acpx run: {shlex.join(run_cmd[:-1] + ['<task>'])}")
        touch_active()
        lock_wait = float(os.environ.get("BOT_ACPX_LOCK_WAIT", "30") or 30)
        with acpx_lock(timeout=lock_wait, holder=f"user:{uid}") as got_lock:
            if not got_lock:
                with runtimeStatusLock:
                    st = runtimeStatus.get(uid, {})
                    st.update({"active": False, "last_error_ts": int(time.time())})
                    runtimeStatus[uid] = st
                return (
                    "⏳ Агент сейчас занят другой задачей, попробуйте через минуту.",
                    {"prompt_tokens": 0, "completion_tokens": 0},
                    {"finish_reason": "acpx_busy", "tool_calls_total": 0, "error": "lock_busy", "session_id": session_uuid},
                )
            r = subprocess.run(run_cmd, capture_output=True, text=True, timeout=180, env=env)
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        raw = ((r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")).strip()
        raw_log = f"{user_dir}/.acpx-last-raw.log"
        try:
            Path(raw_log).write_text(raw)
        except Exception:
            pass

        if r.returncode == 0 and out:
            with runtimeStatusLock:
                st = runtimeStatus.get(uid, {})
                st.update({"active": False, "last_ok_ts": int(time.time())})
                runtimeStatus[uid] = st
            return out, {"prompt_tokens": 0, "completion_tokens": 0}, {"finish_reason": f"acpx_{agent}", "tool_calls_total": 0, "error": None, "session_id": session_uuid}
        msg = err or out or f"acpx prompt failed with exit {r.returncode}"
        msg = f"{msg} [raw: {raw_log}]"
        with runtimeStatusLock:
            st = runtimeStatus.get(uid, {})
            st.update({"active": False, "last_error_ts": int(time.time())})
            runtimeStatus[uid] = st
        return f"❌ ACP mode error: {msg[:1200]}", {"prompt_tokens": 0, "completion_tokens": 0}, {"finish_reason": "acpx_error", "tool_calls_total": 0, "error": msg[:200], "session_id": session_uuid}
    except FileNotFoundError:
        with runtimeStatusLock:
            st = runtimeStatus.get(uid, {})
            st.update({"active": False, "last_error_ts": int(time.time())})
            runtimeStatus[uid] = st
        return "❌ ACP mode is enabled, but `acpx` is not installed on server yet.", {"prompt_tokens": 0, "completion_tokens": 0}, {"finish_reason": "acpx_missing", "tool_calls_total": 0, "error": "acpx_missing", "session_id": locals().get("session_uuid", "")}
    except subprocess.TimeoutExpired:
        with runtimeStatusLock:
            st = runtimeStatus.get(uid, {})
            st.update({"active": False, "last_error_ts": int(time.time())})
            runtimeStatus[uid] = st
        return "❌ ACP mode timed out.", {"prompt_tokens": 0, "completion_tokens": 0}, {"finish_reason": "acpx_timeout", "tool_calls_total": 0, "error": "timeout", "session_id": locals().get("session_uuid", "")}
    except Exception as e:
        with runtimeStatusLock:
            st = runtimeStatus.get(uid, {})
            st.update({"active": False, "last_error_ts": int(time.time())})
            runtimeStatus[uid] = st
        return f"❌ ACP mode exception: {e}", {"prompt_tokens": 0, "completion_tokens": 0}, {"finish_reason": "acpx_exception", "tool_calls_total": 0, "error": str(e)[:200], "session_id": locals().get("session_uuid", "")}

def build_help_text(sess, is_admin=False):
    is_en = sess.get("ui_lang", "ru") == "en"
    if is_en:
        lines = [
            "Hi. Send a message and I will answer with the selected text model.",
            "",
            "Use /menu to choose a model, STT/TTS modes, VideoDetect if available, and settings.",
            "Use /help to show this message.",
        ]
        if is_admin:
            lines.append("Admin tools are in /menu -> Admin.")
        return "\n".join(lines)
    lines = [
        "Привет. Просто напиши сообщение — я отвечу выбранной текстовой моделью.",
        "",
        "Через /menu можно выбрать модель, STT/TTS режимы, VideoDetect если доступен, и настройки.",
        "Через /help показывается эта подсказка.",
    ]
    if is_admin:
        lines.append("Админские инструменты находятся в /menu -> Admin.")
    return "\n".join(lines)

def send_status_text(token, uid):
    sess = DB.get_session(uid)
    ctx_tokens = estimate_tokens(sess["history"])
    ctx_pct = int((ctx_tokens / MAX_CONTEXT_TOKENS) * 100) if MAX_CONTEXT_TOKENS else 0
    with runtimeStatusLock:
        st = dict(runtimeStatus.get(uid, {}))
    with inflightUsersLock:
        inflight = uid in inflightUsers
    active = "да" if (st.get("active") or inflight) else "нет"
    mode = sess.get("engine_mode", "native")
    sid = st.get("active_session_id") or st.get("last_session_id") or sess.get("last_session_id")
    if mode == "native" and not sid:
        sid = uuid.uuid4().hex
        DB.set_last_session_id(uid, sid)
        with runtimeStatusLock:
            st_now = dict(runtimeStatus.get(uid, {}))
            st_now["last_session_id"] = sid
            runtimeStatus[uid] = st_now
    sid_text = sid if sid else "нет (новая сессия — UUID появится после первого ответа)"
    provider = sess.get("provider", PROVIDER_DEFAULT)
    rl = st.get("last_rate_limits") or {}
    rl_provider = st.get("last_rate_limits_provider", "")
    rl_text = "n/a"
    if provider == "openrouter":
        or_limits = fetch_openrouter_key_limits(load_provider_key("openrouter"))
        if or_limits:
            parts = []
            tier = "free" if or_limits.get("is_free_tier") else "paid"
            parts.append(f"tier={tier}")
            usage = or_limits.get("usage")
            if isinstance(usage, (int, float)):
                parts.append(f"usage=${usage:.2f}")
            for k in ("limit_remaining", "credits_remaining"):
                v = or_limits.get(k)
                if isinstance(v, (int, float)):
                    parts.append(f"{k}=${v:.2f}")
            rl_text = ", ".join(parts)
        else:
            rl_text = "n/a (openrouter key-limits unavailable)"
    elif rl and rl_provider == provider:
        rl_text = ", ".join([f"{k}={v}" for k, v in sorted(rl.items())])
    elif provider in ("groq", "cerebras"):
        rl_text = "n/a (send one request with this provider to populate headers)"
    elif provider in ("nvidia", "huggingface"):
        rl_text = "n/a (provider typically does not expose quota headers)"
    txt = (
        "📌 Текущий статус\n"
        f"• Провайдер: `{sess['provider']}`\n"
        f"• Модель: `{sess['model']}`\n"
        f"• Возможности: {', '.join(capabilities_for_model(sess['provider'], sess['model']))}\n"
        f"• Режим: `{mode}`\n"
        f"• Tools: {'on' if sess.get('tools_enabled', True) else 'off'}\n"
        f"• Контекст: {ctx_tokens}/{MAX_CONTEXT_TOKENS} ({ctx_pct}%)\n"
        f"• Session UUID: {sid_text}\n"
        f"• Активный запрос: {active}\n"
        f"• Лимиты: {rl_text}\n"
        f"• Версия: `{__VERSION__}`"
    )
    txt_parsed, ents = parse_markdown_to_entities(txt)
    res = tg_send_text(token, uid, txt_parsed, entities=ents)
    log.info(f"status sendMessage result: ok={res.get('ok')} chat_id={uid} desc={(res.get('description') or '')[:200]}")

def send_users_text(token, uid, admin_id):
    if uid != admin_id:
        return
    stats = DB.get_all_users_stats()
    txt = "👥 *Users:*\n"
    for s in stats:
        role = "👑" if s["id"] == admin_id else ("✅" if s["allowed"] else "❌")
        uname = s["username"] or "Unknown"
        txt += f"• `{s['id']}` (@{uname}): {role} | Msg: {s['count']} | Tkn: {s['prompt']+s['completion']}\n"
    txt_parsed, ents = parse_markdown_to_entities(txt)
    tg_request(token, "sendMessage", {"chat_id": uid, "text": txt_parsed, "entities": ents})

def send_tts_audio(token, uid, source_text):
    from agent.telegram_api import tg_send_chat_action
    tg_send_chat_action(token, uid, action="upload_document")
    sess = DB.get_session(uid)
    try:
        t0 = time.time()
        source_text = source_text.strip()
        tts_model = sess.get("model", "")
        used_provider = sess.get("provider", PROVIDER_DEFAULT)
        if not any(k in (tts_model or "").lower() for k in ("orpheus", "tts", "voice", "speech")):
            picked_prov, picked_model = DB.pick_default_tts_model()
            if picked_model:
                tts_model = picked_model
                used_provider = picked_prov
            else:
                tts_model = "canopylabs/orpheus-v1-english"
                used_provider = "groq"
        audio, used_provider, used_model = tts_with_fallback(used_provider, source_text, tts_model)
        latency_ms = int((time.time() - t0) * 1000)
        res = tg_send_document_bytes(token, uid, "tts.wav", audio, caption="🔊 TTS")
        DB.log_media_request(
            uid,
            used_provider,
            used_model,
            "tts",
            input_size_bytes=len(source_text.encode("utf-8")),
            output_size_bytes=len(audio or b""),
            latency_ms=latency_ms,
            ok=bool(res.get("ok")),
            error=None if res.get("ok") else (res.get("description") or "telegram_send_failed"),
        )
        if not res.get("ok"):
            tg_send_text(token, uid, f"❌ TTS send failed: {(res.get('description') or '')[:200]}")
    except Exception as e:
        DB.log_media_request(
            uid,
            sess.get("provider", PROVIDER_DEFAULT),
            sess.get("model", ""),
            "tts",
            input_size_bytes=len(source_text.encode("utf-8")) if source_text else 0,
            output_size_bytes=0,
            latency_ms=0,
            ok=False,
            error=str(e),
        )
        tg_send_text(token, uid, f"❌ TTS error: {str(e)[:300]}")

def handle_callback(cb, token, admin_id):
    uid = cb["from"]["id"]; data = cb.get("data", "")
    log.info(f"Callback from {uid}: {data}")
    if data.startswith("set_provider:"):
        prov_name = data.split(":", 1)[1]; sess = DB.get_session(uid)
        default_model = DB.pick_default_text_model(prov_name) or PROVIDERS[prov_name]["default_model"]
        default_tools = PROVIDERS[prov_name].get("supports_tools", True)
        DB.save_session(uid, default_model, sess["history"], provider=prov_name, tools_enabled=default_tools, engine_mode=sess.get("engine_mode", "native"))
        tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": f"✅ Provider: {prov_name}\nModel: {default_model}\nTools: {'on' if default_tools else 'off'}"})
    elif data.startswith("set_model:"):
        m = sanitize_model_id(data.split(":", 1)[1])
        sess = DB.get_session(uid)
        info = DB.get_model_info(sess["provider"], m)
        # Reject non-chat categories: audio/image/video/embedding/safety/translation are
        # used via /stt /tts /video flows. Only block when category is known and non-chat —
        # missing health-check rows shouldn't prevent picking a fresh model.
        cat = (info or {}).get("category") or ""
        if cat and cat not in ("text", "code"):
            tg_request(token, "answerCallbackQuery", {
                "callback_query_id": cb["id"],
                "text": f"Эта модель в категории {cat}. Выбор текстовых моделей доступен через /menu.",
                "show_alert": True,
            })
            return
        DB.save_session(uid, m, sess["history"], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode=sess.get("engine_mode", "native"))
        txt = f"✅ Model: {m}"
        if info:
            status = "🟢 Online" if info["available"] else "🔴 Offline"
            tools = "🛠 Tools" if info["supports_tools"] else "❌ No tools"
            cat = info["category"]
            latency = f"{info['latency_ms']}ms" if info["latency_ms"] else "—"
            ago = int(time.time()) - info["last_check"]
            if ago < 60: checked = "just now"
            elif ago < 3600: checked = f"{ago // 60}m ago"
            else: checked = f"{ago // 3600}h ago"
            txt += f"\n{status} | {tools} | {cat}\nLatency: {latency} | Checked: {checked}"
        res = tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": txt})
        # Telegram returns "message is not modified" when user taps the already selected model.
        if not res.get("ok"):
            desc = (res.get("description") or "").lower()
            if "message is not modified" in desc:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Model already selected"})
            else:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Failed to update model", "show_alert": True})
        else:
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Model updated"})
    elif data.startswith("models_cat:"):
        cat = data.split(":", 1)[1].strip().lower()
        sess = DB.get_session(uid)
        txt, kb = build_models_view(sess, category=cat, limit=12)
        res = tg_request(token, "editMessageText", {
            "chat_id": cb["message"]["chat"]["id"],
            "message_id": cb["message"]["message_id"],
            "text": txt,
            "reply_markup": {"inline_keyboard": kb},
        })
        if not res.get("ok"):
            desc = (res.get("description") or "").lower()
            if "message is not modified" in desc:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Already on this filter"})
            else:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Failed to update list", "show_alert": True})
        else:
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": f"Category: {cat}"})
    elif uid == admin_id and data.startswith("approve:"):
        t_uid = int(data.split(":")[1]); DB.set_allowed(t_uid, True)
        tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": f"✅ Approved {t_uid}"})
        tg_request(token, "sendMessage", {"chat_id": t_uid, "text": "✅ Доступ одобрен. Теперь можно пользоваться ботом."})
    elif uid == admin_id and data.startswith("deny:"):
        t_uid = int(data.split(":")[1]); DB.set_allowed(t_uid, False)
        tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": f"❌ Denied {t_uid}"})
        tg_request(token, "sendMessage", {"chat_id": t_uid, "text": "❌ В доступе отказано."})
    elif data == "check_sub":
        if is_subscribed(token, uid):
            DB.set_allowed(uid, True)
            tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": "✅ Verified!"})
        else: tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "❌ Not subscribed!", "show_alert": True})
    elif data == "request_access":
        if is_subscribed(token, uid):
            DB.set_allowed(uid, True)
            tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": "✅ Verified!"})
            return
        uname = cb.get("from", {}).get("username") or f"{cb.get('from', {}).get('first_name', '')} {cb.get('from', {}).get('last_name', '')}".strip()
        uname = f"@{uname}" if uname and not str(uname).startswith("@") else (uname or f"ID: {uid}")
        admin_kb = [[
            {"text": f"✅ Approve {uid}", "callback_data": f"approve:{uid}"},
            {"text": f"❌ Deny {uid}", "callback_data": f"deny:{uid}"}
        ]]
        txt_parsed, ents = parse_markdown_to_entities(f"🔔 New user {uname} (`{uid}`) wants access.")
        tg_request(token, "sendMessage", {"chat_id": admin_id, "text": txt_parsed, "entities": ents, "reply_markup": {"inline_keyboard": admin_kb}})
        tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Запрос отправлен админу"})
    elif data.startswith("set_debug:"):
        if uid != admin_id:
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Недоступно", "show_alert": True})
            return
        mode = data.split(":", 1)[1]
        with DEBUG_USERS_LOCK:
            if mode == "on": DEBUG_USERS.add(uid)
            elif uid in DEBUG_USERS: DEBUG_USERS.remove(uid)
        tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": f"✅ Debug footer: {mode.upper()}"})
    elif data.startswith("set_mode:"):
        if uid != admin_id:
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Недоступно", "show_alert": True})
            return
        mode = data.split(":", 1)[1]
        sess = DB.get_session(uid)
        DB.save_session(uid, sess["model"], sess["history"], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode=mode)
        tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": f"✅ Mode: {mode}"})
    elif data.startswith("set_tools:"):
        if uid != admin_id:
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Недоступно", "show_alert": True})
            return
        mode = data.split(":", 1)[1]
        sess = DB.get_session(uid)
        enabled = (mode == "on")
        if enabled and not PROVIDERS.get(sess["provider"], {}).get("supports_tools", True):
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": f"❌ Provider {sess['provider']} does not support tools.", "show_alert": True})
        else:
            DB.save_session(uid, sess["model"], sess["history"], provider=sess["provider"], tools_enabled=enabled, engine_mode=sess.get("engine_mode", "native"))
            tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": f"✅ Tools: {mode}"})
    elif data == "reset_context":
        sess = DB.get_session(uid)
        DB.save_session(uid, sess["model"], [], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode=sess.get("engine_mode", "native"))
        DB.set_last_session_id(uid, "")
        with runtimeStatusLock:
            runtimeStatus.pop(uid, None)
        u_dir = os.path.join(SESSIONS_ROOT, str(uid))
        if os.path.exists(u_dir): shutil.rmtree(u_dir); os.makedirs(u_dir)
        tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": cb["message"].get("text", "") + "\n\n✅ Context reset done."})
        tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Reset done."})
    elif data.startswith("menu:"):
        action = data.split(":", 1)[1]
        chat_id = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]
        sess = DB.get_session(uid)
        is_en = sess.get("ui_lang", "ru") == "en"
        back_label = "← Back" if is_en else "← Назад"
        if action == "back":
            m_txt, m_kb = build_menu_root(sess, is_admin=(uid == admin_id))
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": m_txt, "reply_markup": {"inline_keyboard": m_kb}})
        elif action == "settings":
            s_txt, s_kb = build_menu_settings(sess, is_admin=(uid == admin_id))
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": s_txt, "reply_markup": {"inline_keyboard": s_kb}})
        elif action == "admin" and uid == admin_id:
            a_txt, a_kb = build_admin_menu(sess)
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": a_txt, "reply_markup": {"inline_keyboard": a_kb}})
        elif action == "chat":
            use_provider, use_model, switched = ensure_text_model_for_session(sess)
            DB.save_session(uid, use_model, sess["history"], provider=use_provider, tools_enabled=sess["tools_enabled"], engine_mode="native")
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": ("💬 Chat mode enabled.\nSend text and I'll reply." if is_en else "💬 Чат-режим включён.\nПиши обычный текст — я отвечу."), "reply_markup": {"inline_keyboard": [[{"text": back_label, "callback_data": "menu:back"}]]}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": ("Switched to text model" if switched and is_en else ("Переключил на текстовую модель" if switched else "Чат"))})
        elif action == "code":
            if uid != admin_id:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Недоступно", "show_alert": True})
                return
            use_provider, use_model, switched = ensure_text_model_for_session(sess)
            DB.save_session(uid, use_model, sess["history"], provider=use_provider, tools_enabled=sess["tools_enabled"], engine_mode="claude")
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": ("🛠 Code mode (Claude Code) enabled.\nSend a task and I'll run it in sandbox." if is_en else "🛠 Код-режим (Claude Code) включён.\nОтправь задачу — выполню в песочнице."), "reply_markup": {"inline_keyboard": [[{"text": back_label, "callback_data": "menu:back"}]]}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": ("Switched to text model" if switched and is_en else ("Переключил на текстовую модель" if switched else "Код"))})
        elif action in ("voice", "stt"):
            if not has_stt_models():
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "STT сейчас недоступен", "show_alert": True})
                return
            with pendingSttUsersLock:
                pendingSttUsers.add(uid)
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": ("🎙 Speech-to-text enabled.\nSend a voice/audio message." if is_en else "🎙 Речь в текст включена.\nПришли голосовое или аудиофайл."), "reply_markup": {"inline_keyboard": [[{"text": back_label, "callback_data": "menu:back"}]]}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "STT"})
        elif action == "tts":
            if not has_tts_models():
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "TTS сейчас недоступен", "show_alert": True})
                return
            with pendingTtsUsersLock:
                pendingTtsUsers.add(uid)
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": ("🔊 Text-to-speech enabled.\nSend text and I will return audio." if is_en else "🔊 Текст в речь включён.\nПришли текст — верну аудио."), "reply_markup": {"inline_keyboard": [[{"text": back_label, "callback_data": "menu:back"}]]}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "TTS"})
        elif action == "image":
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Image-моделей сейчас нет", "show_alert": True})
        elif action == "video":
            video_provider, video_model = pick_video_detector()
            if not video_model:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "VideoDetect сейчас недоступен", "show_alert": True})
                return
            DB.save_session(uid, video_model, sess["history"], provider=video_provider, tools_enabled=sess["tools_enabled"], engine_mode=sess.get("engine_mode", "native"))
            with pendingVideoUsersLock:
                pendingVideoUsers.add(uid)
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": (f"🕵️ VideoDetect enabled.\nModel: {video_model}\nSend video file (you can add text in caption)." if is_en else f"🕵️ VideoDetect включён.\nМодель: {video_model}\nПришли видеофайл (можно с подписью-текстом)."), "reply_markup": {"inline_keyboard": [[{"text": back_label, "callback_data": "menu:back"}]]}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "VideoDetect"})
        elif action == "lang_toggle":
            new_lang = "en" if sess.get("ui_lang", "ru") == "ru" else "ru"
            DB.save_session(uid, sess["model"], sess["history"], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode=sess.get("engine_mode", "native"), ui_lang=new_lang)
            sess["ui_lang"] = new_lang
            s_txt, s_kb = build_menu_settings(sess, is_admin=(uid == admin_id))
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": s_txt, "reply_markup": {"inline_keyboard": s_kb}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": f"Language: {'EN' if new_lang == 'en' else 'RU'}"})

        elif action == "model":
            m_txt, m_kb = build_models_view(sess, category="text", limit=12)
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": m_txt, "reply_markup": {"inline_keyboard": m_kb}})
        elif action == "provider":
            avail = available_providers()
            kb = [[{"text": f"{'✅ ' if name == sess['provider'] else ''}{name}", "callback_data": f"set_provider:{name}"}] for name in avail]
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "Provider:", "reply_markup": {"inline_keyboard": kb}})
        elif action == "reset":
            DB.save_session(uid, sess["model"], [], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode=sess.get("engine_mode", "native"))
            DB.set_last_session_id(uid, "")
            with runtimeStatusLock:
                runtimeStatus.pop(uid, None)
            u_dir = os.path.join(SESSIONS_ROOT, str(uid))
            if os.path.exists(u_dir): shutil.rmtree(u_dir); os.makedirs(u_dir)
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": ("✅ History reset." if is_en else "✅ История сброшена."), "reply_markup": {"inline_keyboard": [[{"text": back_label, "callback_data": "menu:back"}]]}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Сброшено"})
        elif action == "status":
            if uid != admin_id:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Недоступно", "show_alert": True})
                return
            send_status_text(token, uid)
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Статус отправлен"})
        elif action == "help":
            tg_request(token, "sendMessage", {"chat_id": uid, "text": build_help_text(sess, is_admin=(uid == admin_id))})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Помощь отправлена"})
        elif action == "feedback":
            tg_request(token, "sendMessage", {"chat_id": uid, "text": "📝 Чтобы отправить отзыв админу, напиши:\n/feedback <текст сообщения>"})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Инструкция отправлена"})
        elif action == "top":
            if uid != admin_id:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Недоступно", "show_alert": True})
                return
            tg_send_text(token, uid, build_top_text())
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Топ отправлен"})
        elif action == "mode":
            if uid != admin_id:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Недоступно", "show_alert": True})
                return
            kb = [
                [{"text": f"{'✅ ' if sess.get('engine_mode', 'native') == 'native' else ''}Native", "callback_data": "set_mode:native"}],
                [{"text": f"{'✅ ' if sess.get('engine_mode', 'native') == 'claude' else ''}Claude Code", "callback_data": "set_mode:claude"}],
                [{"text": f"{'✅ ' if sess.get('engine_mode', 'native') == 'opencode' else ''}OpenCode", "callback_data": "set_mode:opencode"}],
                [{"text": f"{'✅ ' if sess.get('engine_mode', 'native') == 'pi' else ''}Pi", "callback_data": "set_mode:pi"}],
            ]
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "Select mode:", "reply_markup": {"inline_keyboard": kb}})
        elif action == "tools":
            if uid != admin_id:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Недоступно", "show_alert": True})
                return
            kb = [[{"text": f"{'✅ ' if sess.get('tools_enabled', True) else ''}On", "callback_data": "set_tools:on"},
                   {"text": f"{'✅ ' if not sess.get('tools_enabled', True) else ''}Off", "callback_data": "set_tools:off"}]]
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "Tools usage:", "reply_markup": {"inline_keyboard": kb}})
        elif action == "debug":
            if uid != admin_id:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Недоступно", "show_alert": True})
                return
            with DEBUG_USERS_LOCK:
                is_on = uid in DEBUG_USERS
            kb = [[{"text": f"{'✅ ' if is_on else ''}On", "callback_data": "set_debug:on"},
                   {"text": f"{'✅ ' if not is_on else ''}Off", "callback_data": "set_debug:off"}]]
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "Debug footer settings:", "reply_markup": {"inline_keyboard": kb}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Debug отправлен"})
        elif action == "users":
            if uid != admin_id:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Недоступно", "show_alert": True})
                return
            send_users_text(token, uid, admin_id)
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Users отправлены"})

def handle_command(uid, username, text, token, admin_id):
    cmd = text.split(maxsplit=1)[0].lower()
    sess = DB.get_session(uid)
    if cmd == "/menu" or cmd == "/start":
        sess = DB.get_session(uid)
        m_txt, m_kb = build_menu_root(sess, is_admin=(uid == admin_id))
        tg_request(token, "sendMessage", {"chat_id": uid, "text": m_txt, "reply_markup": {"inline_keyboard": m_kb}})
    elif cmd == "/help":
        tg_request(token, "sendMessage", {"chat_id": uid, "text": build_help_text(sess, is_admin=(uid == admin_id))})
    else:
        tg_send_text(token, uid, "Slash-команды убраны. Используй /menu для действий и /help для подсказки.")
    return True


def build_top_text():
    top_models = DB.get_top_models(limit=3)
    top_providers = DB.get_top_providers(limit=3)
    if not top_models and not top_providers:
        return "Пока нет данных для топа."
    txt = (
        "📊 Top Stats\n"
        "score = success_rate x log10(total + 1), сортировка по score убыв.\n"
        "delivered = успешная доставка ответа пользователю (text + stt/tts)\n\n"
        "🏆 Top 3 Models\n"
    )
    for i, item in enumerate(top_models, start=1):
        txt += (
            f"{i}. {item['provider']}/{item['model']}\n"
            f"   score: {item.get('score', 0):.2f} | delivered: {item['delivered']} | total: {item['total']} | success: {item['success_rate']:.1f}%\n"
        )
    txt += "\n🏅 Top 3 Providers\n"
    for i, item in enumerate(top_providers, start=1):
        txt += (
            f"{i}. {item['provider']}\n"
            f"   score: {item.get('score', 0):.2f} | delivered: {item['delivered']} | total: {item['total']} | success: {item['success_rate']:.1f}%\n"
        )
    return txt

def process_update(upd, token, admin_id):
    try:
        upd_id = upd.get("update_id")
        if upd_id is not None:
            now_ts = time.time()
            with recentUpdateIdsLock:
                # Cleanup stale ids and reject duplicates from Telegram webhook retries.
                stale_ids = [k for k, ts in recentUpdateIds.items() if now_ts - ts > RECENT_UPDATE_TTL_SEC]
                for k in stale_ids:
                    recentUpdateIds.pop(k, None)
                if upd_id in recentUpdateIds:
                    log.info(f"Duplicate update ignored: {upd_id}")
                    return
                recentUpdateIds[upd_id] = now_ts
        cb = upd.get("callback_query")
        if cb: return handle_callback(cb, token, admin_id)
        msg = upd.get("message")
        if not msg: return
        fi = msg.get("from") or {}
        uid = fi.get("id")
        if uid is None:
            log.warning(f"Skipping message without sender info. Keys: {list(msg.keys())}")
            return
        # STT path: accept incoming voice/audio/document when either:
        # 1) user explicitly requested /stt, or
        # 2) selected model is an audio/STT model (auto mode).
        with pendingSttUsersLock:
            stt_pending = uid in pendingSttUsers
        with pendingVideoUsersLock:
            video_pending = uid in pendingVideoUsers
        sess_for_media = DB.get_session(uid)
        model_for_media = (sess_for_media.get("model") or "").lower()
        model_caps = capabilities_for_model(sess_for_media.get("provider", PROVIDER_DEFAULT), sess_for_media.get("model", ""))
        auto_stt_model = any(k in model_for_media for k in ("whisper", "speech-to-text", "stt"))
        has_video_detector = "video:detect" in model_caps
        has_video_payload = (
            ("video" in msg)
            or ("animation" in msg)
            or ("document" in msg and (
                str((msg.get("document") or {}).get("mime_type", "")).lower().startswith("video/")
                or str((msg.get("document") or {}).get("mime_type", "")).lower() == "image/gif"
            ))
        )

        if (has_video_detector and has_video_payload) or (video_pending and has_video_payload):
            try:
                provider = sess_for_media.get("provider", PROVIDER_DEFAULT)
                selected_model = sess_for_media.get("model", "")
                model_info = DB.get_model_info(provider, selected_model)
                if model_info and not model_info.get("available", False):
                    ago = int(time.time()) - int(model_info.get("last_check") or 0)
                    if ago < 60:
                        checked = "just now"
                    elif ago < 3600:
                        checked = f"{ago // 60}m ago"
                    else:
                        checked = f"{ago // 3600}h ago"
                    tg_send_text(
                        token,
                        uid,
                        f"❌ Selected detector model is offline now ({provider}/{selected_model}). Last check: {checked}. Choose another model in /models -> VideoDetect.",
                    )
                    return
                media = msg.get("video") or msg.get("animation") or msg.get("document")
                mime_type = str((media or {}).get("mime_type", "")).lower()
                file_name = str((media or {}).get("file_name", "")).lower()
                if mime_type == "image/gif" or file_name.endswith(".gif"):
                    tg_send_text(token, uid, "❌ GIF is not supported for video detection by current provider endpoint. Send MP4/WebM video file.")
                    return
                media_size = int(media.get("file_size") or 0)
                if media_size > TELEGRAM_BOT_FILE_DOWNLOAD_LIMIT_BYTES:
                    limit = format_bytes(TELEGRAM_BOT_FILE_DOWNLOAD_LIMIT_BYTES)
                    got = format_bytes(media_size)
                    DB.log_media_request(
                        uid,
                        sess_for_media.get("provider", PROVIDER_DEFAULT),
                        sess_for_media.get("model", ""),
                        "video_detect",
                        input_size_bytes=media_size,
                        output_size_bytes=0,
                        latency_ms=0,
                        ok=False,
                        error=f"file_too_big:{media_size}",
                    )
                    tg_send_text(token, uid, f"❌ Video is too big for Telegram Bot API download ({got} > {limit}). Send a smaller/compressed file.")
                    return
                file_id = media.get("file_id")
                from agent.telegram_api import tg_send_chat_action
                tg_send_chat_action(token, uid, action="typing")
                t0 = time.time()
                file_path, blob = tg_get_file_bytes(token, file_id)
                prov = PROVIDERS.get(provider, PROVIDERS[PROVIDER_DEFAULT])
                api_key = load_provider_key(provider) or load_provider_key(PROVIDER_DEFAULT)
                if not api_key:
                    raise RuntimeError(f"No API key configured for provider {provider}")
                analysis = analyze_video_detection(
                    prov["url"],
                    api_key,
                    selected_model,
                    blob,
                    filename=os.path.basename(file_path or "video.mp4"),
                    use_proxy=prov.get("proxy", False),
                )
                latency_ms = int((time.time() - t0) * 1000)
                DB.log_media_request(
                    uid,
                    provider,
                    selected_model,
                    "video_detect",
                    input_size_bytes=len(blob or b""),
                    output_size_bytes=len((analysis or "").encode("utf-8")),
                    latency_ms=latency_ms,
                    ok=bool(analysis),
                    error=None if analysis else "empty_analysis",
                )
                caption_text = str((msg.get("caption") or "")).strip()
                lang = sess_for_media.get("ui_lang", "ru")
                if analysis:
                    tg_send_long_text(token, uid, format_video_analysis(analysis, lang=lang, caption_text=caption_text))
                else:
                    tg_send_text(token, uid, "⚠️ Empty video analysis result.")
            except Exception as e:
                DB.log_media_request(
                    uid,
                    sess_for_media.get("provider", PROVIDER_DEFAULT),
                    sess_for_media.get("model", ""),
                    "video_detect",
                    input_size_bytes=0,
                    output_size_bytes=0,
                    latency_ms=0,
                    ok=False,
                    error=str(e),
                )
                tg_send_text(token, uid, f"❌ Video analysis error: {str(e)[:300]}")
            finally:
                with pendingVideoUsersLock:
                    pendingVideoUsers.discard(uid)
            return

        if (stt_pending or auto_stt_model) and ("voice" in msg or "audio" in msg or "document" in msg):
            try:
                media = msg.get("voice") or msg.get("audio") or msg.get("document")
                media_size = int(media.get("file_size") or 0)
                if media_size > TELEGRAM_BOT_FILE_DOWNLOAD_LIMIT_BYTES:
                    limit = format_bytes(TELEGRAM_BOT_FILE_DOWNLOAD_LIMIT_BYTES)
                    got = format_bytes(media_size)
                    DB.log_media_request(
                        uid,
                        STT_PROVIDER,
                        sess_for_media.get("model", ""),
                        "stt",
                        input_size_bytes=media_size,
                        output_size_bytes=0,
                        latency_ms=0,
                        ok=False,
                        error=f"file_too_big:{media_size}",
                    )
                    tg_send_text(token, uid, f"❌ Audio is too big for Telegram Bot API download ({got} > {limit}). Send a shorter/compressed file.")
                    return
                file_id = media.get("file_id")
                from agent.telegram_api import tg_send_chat_action
                tg_send_chat_action(token, uid, action="typing")
                t0 = time.time()
                file_path, blob = tg_get_file_bytes(token, file_id)
                stt_model = sess_for_media.get("model", "")
                used_provider = sess_for_media.get("provider", PROVIDER_DEFAULT)
                # If the active chat model isn't an STT/audio model, pick the
                # fastest healthy STT model from model_health. Otherwise we'd
                # pass e.g. qwen/qwen3-32b to groq and get HTTP 400.
                if not any(k in (stt_model or "").lower() for k in ("whisper", "speech-to-text", "stt")):
                    picked_prov, picked_model = DB.pick_default_stt_model()
                    if picked_model:
                        stt_model = picked_model
                        used_provider = picked_prov
                    else:
                        stt_model = "whisper-large-v3-turbo"
                        used_provider = "groq"
                transcript, used_provider, used_model = transcribe_audio_with_fallback(
                    used_provider,
                    blob,
                    os.path.basename(file_path or "audio.ogg"),
                    stt_model,
                )
                latency_ms = int((time.time() - t0) * 1000)
                DB.log_media_request(
                    uid,
                    used_provider,
                    used_model,
                    "stt",
                    input_size_bytes=len(blob or b""),
                    output_size_bytes=len((transcript or "").encode("utf-8")),
                    latency_ms=latency_ms,
                    ok=bool(transcript),
                    error=None if transcript else "empty_transcription",
                )
                if transcript:
                    tg_send_long_text(token, uid, f"📝 Transcription:\n{transcript}")
                else:
                    tg_send_text(token, uid, "⚠️ No transcription text returned.")
            except Exception as e:
                DB.log_media_request(
                    uid,
                    sess_for_media.get("provider", PROVIDER_DEFAULT),
                    sess_for_media.get("model", ""),
                    "stt",
                    input_size_bytes=0,
                    output_size_bytes=0,
                    latency_ms=0,
                    ok=False,
                    error=str(e),
                )
                tg_send_text(token, uid, f"❌ STT error: {str(e)[:300]}")
            finally:
                with pendingSttUsersLock:
                    pendingSttUsers.discard(uid)
            return

        # Extract text from message or convert location/venue to text.
        if "text" in msg:
            text = msg["text"].strip()
        elif "location" in msg:
            loc = msg["location"]
            lat, lon = loc["latitude"], loc["longitude"]
            text = f"[Геолокация: {lat}, {lon}]"
        elif "venue" in msg:
            venue = msg["venue"]
            loc = venue.get("location", {})
            lat, lon = loc.get("latitude", 0), loc.get("longitude", 0)
            title = venue.get("title", "")
            addr = venue.get("address", "")
            text = f"[Место: {title}, {addr}, координаты: {lat}, {lon}]"
        else:
            return
        username = fi.get("username") or f"{fi.get('first_name', '')} {fi.get('last_name', '')}".strip()
        log.info(f"Update from {uid} ({username}): {text}")

        lower_text = text.strip().lower()
        translate_cmds = {
            "переведи на русский",
            "переведи на русский:",
            "translate to russian",
            "translate to russian:",
        }
        with pendingTranslateUsersLock:
            waiting_translate_text = uid in pendingTranslateUsers
        if waiting_translate_text and not lower_text.startswith("/"):
            text = f"Переведи на русский:\n\n{text}"
            with pendingTranslateUsersLock:
                pendingTranslateUsers.discard(uid)
        elif lower_text in translate_cmds:
            with pendingTranslateUsersLock:
                pendingTranslateUsers.add(uid)
            tg_send_text(token, uid, "Ок. Пришлите текст следующим сообщением — переведу на русский без привязки к предыдущей теме.")
            return

        allowed = DB.update_and_check(uid, username)
        if uid == admin_id: allowed = True
        if not allowed:
            if is_subscribed(token, uid): DB.set_allowed(uid, True); allowed = True
            else:
                kb = [
                    [{"text": f"📢 Подписаться на {REQUIRED_CHANNEL}", "url": f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"}],
                    [{"text": "✅ Я подписался — проверить", "callback_data": "check_sub"}],
                    [{"text": "📝 Запросить доступ без подписки", "callback_data": "request_access"}],
                ]
                welcome = (
                    "Привет! Я AI-ассистент.\n\n"
                    "Что умею:\n"
                    "💬 Чат с большими моделями (бесплатные, без VPN)\n"
                    "🎙 Распознавание голосовых сообщений\n"
                    "🔊 Озвучка текста в аудио\n"
                    "🛠 Запуск кода в песочнице\n\n"
                    f"Чтобы начать, подпишись на канал {REQUIRED_CHANNEL} — после этого доступ откроется автоматически. "
                    "Или нажми «Запросить доступ» и я отправлю заявку админу."
                )
                tg_request(token, "sendMessage", {"chat_id": uid, "text": welcome, "reply_markup": {"inline_keyboard": kb}})
                return
        if text.startswith("/"): return handle_command(uid, username, text, token, admin_id)

        with inflightUsersLock:
            if uid in inflightUsers:
                now_ts = time.time()
                last_notice_ts = inflightBusyNoticeTs.get(uid, 0)
                with pendingTextByUserLock:
                    pendingTextByUser[uid] = {
                        "text": text,
                        "from": fi,
                        "chat_id": msg.get("chat", {}).get("id", uid),
                    }
                if now_ts - last_notice_ts >= INFLIGHT_BUSY_NOTICE_COOLDOWN_SEC:
                    tg_send_text(token, uid, "⏳ Previous request is still running. I saved your latest message and will process it next.")
                    inflightBusyNoticeTs[uid] = now_ts
                return
            inflightUsers.add(uid)

        sess = DB.get_session(uid); hist = sess["history"]; model = sess["model"]; provider = sess["provider"]
        fixed_provider, fixed_model, switched_to_text = ensure_text_model_for_session(sess)
        if switched_to_text:
            provider = fixed_provider
            model = fixed_model
            DB.save_session(
                uid,
                model,
                hist,
                provider=provider,
                tools_enabled=sess["tools_enabled"],
                engine_mode=sess.get("engine_mode", "native"),
                ui_lang=sess.get("ui_lang", "ru"),
            )
            if sess.get("ui_lang", "ru") == "en":
                tg_send_text(token, uid, "ℹ️ Switched from media model to chat model for text request.")
            else:
                tg_send_text(token, uid, "ℹ️ Для текстового запроса переключил модель с видео на чатовую.")
        prov = PROVIDERS.get(provider, PROVIDERS[PROVIDER_DEFAULT])
        api_key = load_provider_key(provider) or load_provider_key(PROVIDER_DEFAULT)
        use_proxy = prov.get("proxy", False)
        
        model_caps = capabilities_for_model(provider, model)
        use_tools = sess.get("tools_enabled", True) and prov.get("supports_tools", True) and ("tools" in model_caps)

        if estimate_tokens(hist) > MAX_CONTEXT_TOKENS:
            hist = compact_history(prov["url"], api_key, model, hist, uid, admin_id, use_proxy=use_proxy)

        role_desc = "You are an ADMIN with full internet access." if uid == admin_id else "You are a USER. Internet restricted."
        sys_prompt = f"Smolevich AI Bot. Instructions: {role_desc} Environment: Alpine Linux. No 'requests' lib, use 'urllib.request', wget, curl. Use DuckDuckGo (html.duckduckgo.com) if Google fails. Output: Telegram Markdown V2 — use *bold*, _italic_, `inline code`, triple backticks for code blocks, [text](url) for links. Keep formatting simple and valid for Telegram markdown. Be concise — show actual command output, no hypothetical examples, no tables with status, no 'next steps' sections. Just execute and show results. When user sends coordinates [Геолокация: lat, lon], use them for location-based queries (search nearby places, weather, etc.). Always complete your answer fully — never cut off mid-sentence."

        mode = sess.get("engine_mode", "native")
        from agent.telegram_api import tg_send_chat_action
        tg_send_chat_action(token, uid, action="typing")
        if mode in ("claude", "opencode", "pi"):
            ans, usage, meta = ask_via_acpx(uid, text, sess)
        else:
            prompt_messages = [{"role": "system", "content": sys_prompt}] + hist + [{"role": "user", "content": text}]
            ans, usage, meta = ask_llm(prov["url"], api_key, model,
                                 prompt_messages,
                                 uid=uid, admin_id=admin_id, use_tools=use_tools, use_proxy=use_proxy)
        if ans == "No response":
            ans = "⚠️ Empty model output. Try `/tools off` or choose another model via `/models`."
        DB.add_usage(uid, usage['prompt_tokens'], usage['completion_tokens'])
        req_id = DB.log_request(uid, provider, model, usage['prompt_tokens'], usage['completion_tokens'],
                                meta['finish_reason'], meta['tool_calls_total'], meta['error'], mode=sess.get("engine_mode", "native"), request_http_ms=meta.get("http_latency_ms", 0))
        with runtimeStatusLock:
            st_now = dict(runtimeStatus.get(uid, {}))
            st_now["last_rate_limits"] = meta.get("rate_limits", {}) or {}
            st_now["last_rate_limits_provider"] = provider
            st_now["last_rate_limits_ts"] = int(time.time())
            runtimeStatus[uid] = st_now
        hist.append({"role": "user", "content": text}); hist.append({"role": "assistant", "content": ans})
        # Avoid clobbering mode/tools with stale in-memory session when updates are processed concurrently.
        latest = DB.get_session(uid)
        DB.save_session(
            uid,
            model,
            hist,
            provider=provider,
            tools_enabled=latest.get("tools_enabled", sess.get("tools_enabled", True)),
            engine_mode=latest.get("engine_mode", sess.get("engine_mode", "native")),
        )
        model_short = model.split("/")[-1] if "/" in model else model
        if mode in ("claude", "opencode", "pi"):
            sid = meta.get("session_id", "")
        else:
            # Native mode is stateless per turn API-side, but we want a stable session UUID
            # for /status, the footer, and human reference. Generate once after first
            # successful response and persist to sessions.last_session_id; /reset clears it.
            sid = sess.get("last_session_id") or ""
            if not sid:
                sid = uuid.uuid4().hex
                DB.set_last_session_id(uid, sid)
                with runtimeStatusLock:
                    st_now = dict(runtimeStatus.get(uid, {}))
                    st_now["last_session_id"] = sid
                    runtimeStatus[uid] = st_now
        sid_part = f" | sid: {sid[:8]}" if sid else ""
        raw_reply = ans
        with DEBUG_USERS_LOCK:
            is_debug = uid in DEBUG_USERS
        if is_debug:
            footer = f"[{provider}/{model_short} | In: {usage['prompt_tokens']} | Out: {usage['completion_tokens']} | Ctx: {estimate_tokens(hist)}/{MAX_CONTEXT_TOKENS}{sid_part}]"
            raw_reply += f"\n\n_{footer}_"
            
        kb = None
        if estimate_tokens(hist) > MAX_CONTEXT_TOKENS * 0.5:
            kb = {"inline_keyboard": [[{"text": "🔄 Reset Context", "callback_data": "reset_context"}]]}
            
        txt_parsed, ents = parse_markdown_to_entities(raw_reply)
        send_res = tg_send_long_text(token, uid, txt_parsed, entities=ents, reply_markup=kb)
        DB.set_request_delivered(req_id, bool(send_res.get("ok")))
        queued = None
        with pendingTextByUserLock:
            queued = pendingTextByUser.pop(uid, None)
        with inflightUsersLock:
            inflightUsers.discard(uid)
            inflightBusyNoticeTs.pop(uid, None)
        if queued and queued.get("text"):
            try:
                synthetic_upd = {
                    "update_id": int(time.time() * 1000),
                    "message": {
                        "message_id": int(time.time() * 1000) % 1000000000,
                        "from": queued.get("from") or {"id": uid},
                        "chat": {"id": queued.get("chat_id", uid)},
                        "text": queued.get("text", ""),
                    },
                }
                executorPool.submit(process_update, synthetic_upd, token, admin_id)
            except Exception as e:
                log.warning(f"Failed to schedule queued message for uid={uid}: {e}")
    except Exception as e:
        log.error(f"process_update error: {e}", exc_info=True)
        try:
            if 'uid' in locals() and uid is not None:
                with inflightUsersLock:
                    inflightUsers.discard(uid)
                    inflightBusyNoticeTs.pop(uid, None)
        except Exception:
            pass

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            t = load_bot_token(); a = load_admin(); l = int(self.headers.get("Content-Length", 0))
            if self.path.strip("/") != t:
                log.warning(f"Invalid path: {self.path}")
                self.send_response(403); self.end_headers(); return
            body = self.rfile.read(l).decode(); self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            executorPool.submit(process_update, json.loads(body), t, a)
        except Exception as e: log.error(f"Webhook error: {e}")
    def log_message(self, *args): pass

if __name__ == "__main__":
    t = load_bot_token()
    if t:
        DB.ensure_schema()
        avail = available_providers()
        log.info(f"smolevich-ai-bot starting on port 8080. Providers: {avail}. Webhook: {TUNNEL_URL}/{t[:5]}...")
        set_bot_commands(t)
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{t}/setWebhook", f"--B\r\nContent-Disposition: form-data; name=\"url\"\r\n\r\n{TUNNEL_URL}/{t}\r\n--B--\r\n".encode(), {"Content-Type": "multipart/form-data; boundary=B"}))
        ThreadingHTTPServer(("127.0.0.1", 8080), WebhookHandler).serve_forever()
    else: log.error("No token!")
