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
    PROVIDER_PARKED_UNTIL_HUMAN,
    PROXY_URL,
    REQUIRED_CHANNEL,
    SESSIONS_ROOT,
    TUNNEL_URL,
)
from agent.text import (
    answer_from_message,
    compact_messages_for_provider,
    estimate_tokens,
    sanitize_model_id,
    strip_reasoning,
    unavailable_message,
)
from agent import model_routing, quota
from agent.entities import normalize_list_markers, parse_markdown_to_entities
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
# Когда провайдер расшифровки/озвучки назвал срок возврата — храним его, чтобы
# «сейчас недоступно» могло сказать «через сколько», а не оборвать разговор.
featureRetryAfter = {}
featureRetryAfterLock = threading.Lock()
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

BASH_DESC_ADMIN = "Execute bash in Alpine Linux with network access. No 'requests' library, use urllib.request/wget/curl."
BASH_DESC_USER = "Execute bash in Alpine Linux. NO NETWORK: curl, wget and any download will fail. Local computation only."

TOOLS = [
    {"type": "function", "function": {"name": "execute_bash", "description": BASH_DESC_ADMIN, "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "get_weather", "description": "Get weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}},
    {"type": "function", "function": {"name": "get_exchange_rate", "description": "Get rate", "parameters": {"type": "object", "properties": {"from_currency": {"type": "string"}, "to_currency": {"type": "string"}, "amount": {"type": "number", "default": 1}}, "required": ["from_currency", "to_currency"]}}}
]
TOOL_HANDLERS = {"get_weather": lambda a: tool_get_weather(a["city"]), "get_exchange_rate": lambda a: tool_get_exchange_rate(a["from_currency"], a["to_currency"], a.get("amount", 1))}


def tools_for(is_admin):
    """Same tools for everyone, but a plain user's sandbox has no network — say so in the schema."""
    if is_admin:
        return TOOLS
    tools = json.loads(json.dumps(TOOLS))
    for t in tools:
        if t["function"]["name"] == "execute_bash":
            t["function"]["description"] = BASH_DESC_USER
    return tools

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

def build_models_view(sess, category="text", limit=12, is_admin=False):
    """Who can answer you, in the same words the leaderboard uses.

    The old screen showed `🟢🛠🎙 muse-glimmer-30b 812ms` — four unexplained icons, an
    identifier and milliseconds. None of that answers "which one should I pick".
    """
    is_en = sess.get("ui_lang", "ru") == "en"
    prov = sess["provider"]
    ms = DB.get_recent_models(prov, max_age_sec=1800, category="text", limit=max(limit * 3, 30))
    # Offering a model the last probe called dead only buys the user an error.
    ms = [m for m in ms if m.get("available", True)]
    if not ms:
        ms = DB.get_healthy_models(prov, category="text", limit=max(limit * 3, 30))
    ms = ms[:limit]

    # Scores belong to the admin board: "решает 7 из 10" on a button is a number nobody
    # asked for and cannot act on. The name is enough to recognise what you picked.
    kb = []
    for m in ms:
        mid = m["id"]
        kb.append([{"text": model_routing.human_model_name(mid)[:60], "callback_data": f"set_model:{mid}"}])
    # "Provider" is our plumbing, not a thing a person picks between; only the admin
    # gets the door into it.
    if is_admin:
        kb.append([{"text": ("🔌 Where answers come from" if is_en else "🔌 Откуда брать ответы"), "callback_data": "menu:provider"}])
    kb.append([{"text": ("← Back" if is_en else "← Назад"), "callback_data": "menu:curious"}])
    return ("Who answers you" if is_en else "Кто будет отвечать"), kb


# Ходят по API как `nvidia`, называются иначе. Экран провайдеров админский, но
# идентификаторы на кнопках читать всё равно незачем.
PROVIDER_DISPLAY_NAMES = {"openrouter": "OpenRouter", "groq": "Groq", "nvidia": "NVIDIA"}


def provider_display_name(name):
    return PROVIDER_DISPLAY_NAMES.get(name, (name or "").capitalize())


# Всплывашка от кнопки — такой же текст бота, как ответ в чат. До 09-07 половина
# была на английском и про модели: `Model updated`, `Failed to update model`.
# Админские ветки сюда не входят — им техника нужна.
TOASTS = {
    "done": {"ru": "Готово", "en": "Done"},
    "already": {"ru": "Уже выбрано", "en": "Already chosen"},
    "failed": {"ru": "Не получилось, попробуй ещё раз", "en": "Didn't work, try again"},
    "unavailable": {"ru": "Недоступно", "en": "Unavailable"},
    "not_for_chat": {"ru": "Эта для разговора не годится — она умеет другое",
                     "en": "That one is not for chatting"},
    "chat": {"ru": "Чат", "en": "Chat"},
    "stt": {"ru": "Расшифровка", "en": "Transcription"},
    "tts": {"ru": "Озвучка", "en": "Voicing"},
    "video": {"ru": "Проверка видео", "en": "Video check"},
    "reset": {"ru": "Сброшено", "en": "Reset"},
    "help_sent": {"ru": "Помощь отправлена", "en": "Help sent"},
}


def toast(key, is_en=False):
    """Текст всплывашки по ключу. Неизвестный ключ — пусто, а не английское имя ключа."""
    return TOASTS.get(key, {}).get("en" if is_en else "ru", "")


def say_toast(token, cb_id, key, is_en=False, alert=False):
    payload = {"callback_query_id": cb_id, "text": toast(key, is_en)}
    if alert:
        payload["show_alert"] = True
    return tg_request(token, "answerCallbackQuery", payload)


QUICK_CHAT = {"ru": "💬 Спросить", "en": "💬 Ask"}
# A reply keyboard stays on the client until it is replaced, so a button removed from the
# layout keeps arriving as plain text — and went to the model as a question.
QUICK_MODEL_LEGACY = {"ru": "🤖 Модель", "en": "🤖 Model"}
QUICK_BOARD = {"ru": "🏆 Кто лучше", "en": "🏆 Best models"}
QUICK_STT = {"ru": "🎙 Аудио → текст", "en": "🎙 Audio → text"}
QUICK_TTS = {"ru": "🔊 Текст → аудио", "en": "🔊 Text → audio"}
QUICK_MORE = {"ru": "☰ Ещё", "en": "☰ More"}


def build_quick_keyboard(sess):
    """Bottom reply keyboard: four buttons at most, everything else lives under ☰.

    The measurement used to sit here as "🏆 Кто лучше" — the second thing a person saw
    was a table asking them to pick a model. Asking is the bot's job now, so the board
    moved down to ☰ → «Для любопытных» and the bottom row is: ask, transcribe, the rest.
    """
    lang = "en" if sess.get("ui_lang", "ru") == "en" else "ru"
    kb = [[{"text": QUICK_CHAT[lang]}]]
    # Four buttons is the cap, so only transcription gets a spot here; voicing lives under ☰.
    if has_stt_models():
        kb.append([{"text": QUICK_STT[lang]}])
    kb.append([{"text": QUICK_MORE[lang]}])
    return {"keyboard": kb, "resize_keyboard": True, "is_persistent": True}


def quick_action_for(text):
    """Map a bottom-keyboard label back to an action, in either UI language.

    Matching is exact: `in`/`startswith` would swallow real questions.
    """
    t = (text or "").strip()
    for action, labels in (("chat", QUICK_CHAT), ("board", QUICK_BOARD), ("stt", QUICK_STT),
                           ("tts", QUICK_TTS), ("more", QUICK_MORE), ("model", QUICK_MODEL_LEGACY)):
        if t in labels.values():
            return action
    return ""


def build_menu_root(sess, is_admin=False):
    """Корневое inline-меню под ☰.

    Its title used to be the literal "☰ Ещё" — the same string the bottom button sends,
    so the chat showed the label twice, once from the person and once from the bot, and
    read as if the bot were echoing the tap back.
    """
    ui_lang = sess.get("ui_lang", "ru")
    is_en = ui_lang == "en"
    kb = []
    if has_tts_models():
        kb.append([{"text": ("🔊 Text → audio" if is_en else "🔊 Текст → аудио"), "callback_data": "menu:tts"}])
    if has_video_detector():
        kb.append([{"text": ("🕵️ Is the video AI-made?" if is_en else "🕵️ Видео: AI или нет"), "callback_data": "menu:video"}])
    kb.append([{"text": ("🔬 For the curious" if is_en else "🔬 Для любопытных"), "callback_data": "menu:curious"}])
    kb.append([{"text": ("⚙️ Settings" if is_en else "⚙️ Настройки"), "callback_data": "menu:settings"}])
    if is_admin:
        kb.append([{"text": "🛠 Admin", "callback_data": "menu:admin"}])
    return ("Everything else" if is_en else "Остальные возможности"), kb

def build_menu_settings(sess, is_admin=False):
    """Сабменю настроек. Модель и провайдер сюда не входят — они под «Для любопытных»."""
    is_en = sess.get("ui_lang", "ru") == "en"
    ui_lang = sess.get("ui_lang", "ru")
    lang_label = "RU" if ui_lang == "ru" else "EN"
    kb = [
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
    try:
        with opener.open(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        note_feature_retry_after("stt", retry_after_seconds(e.headers))
        raise
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
    try:
        with opener.open(req, timeout=120) as resp:
            return resp.read(), provider, target_model
    except urllib.error.HTTPError as e:
        note_feature_retry_after("tts", retry_after_seconds(e.headers))
        raise


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

def retry_after_seconds(headers):
    """Seconds until this endpoint is worth trying again, or None when it did not say.

    Providers answer in three dialects: seconds, an epoch in seconds, an epoch in ms.
    A missing header used to end up printed to the user as the literal "(None)".
    """
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("x-ratelimit-reset")
    except Exception:
        return None
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 1e11:
        value /= 1000
    if value > 1e9:
        value -= time.time()
    return max(0.0, value)


def media_too_big_or_wrong_format(reason, got="", limit="", is_en=False):
    """Что прислать и какой предел — всё, что человеку тут нужно.

    Двадцать мегабайт — не наш выбор: столько Telegram отдаёт боту по file_id.
    Название этого ограничения человеку ни о чём не говорит, поэтому названо число.
    """
    if reason == "video_format":
        return ("I can't check GIFs. Send a video file — MP4 or WebM."
                if is_en else "Гифки проверить не могу. Пришли видео файлом — MP4 или WebM.")
    if reason == "audio_size":
        return (f"The file is too big ({got}). I can take up to {limit} — send a shorter one."
                if is_en else f"Файл великоват ({got}). Приму до {limit} — пришли покороче.")
    return (f"The file is too big ({got}). I can take up to {limit} — send a shorter or compressed one."
            if is_en else f"Файл великоват ({got}). Приму до {limit} — пришли покороче или сожми.")


def should_show_debug_footer(uid, admin_id):
    """Подвал с sid, токенами и контекстом — админский, а не «у кого включено»."""
    if uid != admin_id:
        return False
    with DEBUG_USERS_LOCK:
        return uid in DEBUG_USERS


def note_feature_retry_after(kind, seconds, now=None):
    """Запомнить срок, который провайдер назвал в Retry-After для расшифровки/озвучки."""
    if not seconds or seconds <= 0:
        return
    now = time.time() if now is None else now
    with featureRetryAfterLock:
        featureRetryAfter[kind] = now + seconds


def feature_retry_after_sec(kind, now=None):
    """Сколько ещё ждать по последнему ответу провайдера, или None — если он молчал."""
    now = time.time() if now is None else now
    with featureRetryAfterLock:
        deadline = featureRetryAfter.get(kind, 0)
    left = deadline - now
    return left if left > 0 else None


def format_bytes(size: int, is_en: bool = False) -> str:
    """Размер в тех же единицах, что и на экране «Видео: AI или нет» — «20 МБ»."""
    b, kb, mb = ("B", "KB", "MB") if is_en else ("Б", "КБ", "МБ")
    if size < 1024:
        return f"{size} {b}"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} {kb}"
    return f"{size / (1024 * 1024):.1f} {mb}"


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
    meta = {"finish_reason": None, "tool_calls_total": 0, "error": None, "http_latency_ms": 0,
            "rate_limits": {}, "status": None, "retry_after_sec": None}
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
        # OpenRouter returns the chain of thought unless asked not to; other providers 400 on
        # unknown fields, so the switch is scoped to the one API that documents it.
        if "openrouter.ai" in (api_url or ""):
            payload["reasoning"] = {"exclude": True}
        if retry_use_tools: payload.update({"tools": tools_for(uid == admin_id), "tool_choice": "auto"})
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
                    content = answer_from_message(msg)
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
                                        cont_text = answer_from_message(cont_msg)
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
                    # An empty body is a failure like any other: the next model gets a turn.
                    return None, usage, meta
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
            meta["status"] = e.code
            meta["rate_limits"] = extract_rate_limit_headers(e.headers)
            meta["retry_after_sec"] = retry_after_seconds(e.headers)
            err_body = ""
            try:
                err_body = e.read().decode(errors="replace")
            except Exception:
                err_body = ""
            if err_body:
                log.warning(f"HTTP {e.code} from provider for model={model}: {err_body[:400]}")
            # No user-facing text here: the caller knows the fallback chain and decides what,
            # if anything, the human is told. A refusal string returned as an answer is what
            # made the bot say "pick another model" instead of picking one itself.
            return None, usage, meta
        except Exception as e:
            try:
                meta["http_latency_ms"] += int((time.time() - t_http) * 1000)
            except Exception:
                pass
            meta["error"] = str(e)[:200]
            log.error(f"ask_llm error: {e}")
            return None, usage, meta
    meta["error"] = "loop_limit"
    return None, usage, meta

def compact_history(api_url, api_key, model, history, uid, admin_id, use_proxy=False):
    to_sum = history[:-4]; keep = history[-4:]
    p = [{"role": "system", "content": "Summarize concisely."}] + to_sum
    sum_text, _, _ = ask_llm(api_url, api_key, model, p, uid=uid, admin_id=admin_id, use_tools=False, use_proxy=use_proxy)
    if not sum_text:
        # Summarising failed; dropping the old turns still keeps the context under the cap.
        return keep
    return [{"role": "system", "content": f"Summary: {sum_text}"}] + keep


def live_model_ranking():
    """Every model that can answer right now, best first.

    Order: answers solved in the last measurement, then how often the model answered at
    all in the past day. Models the probe could not reach, models a parked provider owns,
    and models that solved zero of ten are not in the list at all.
    """
    now = int(time.time())
    parked = model_routing.parked_providers(DB.get_provider_state(), now)
    usable = set(available_providers()) - parked
    rows = [r for r in DB.get_live_text_rows() if r["provider"] in usable]
    live = model_routing.live_candidates(rows, now, parked=parked)
    latency = {(r["provider"], r["model_id"]): r.get("latency_ms", 0) for r in rows}
    board = ((fetch_leaderboard() or {}).get("models") or [])
    return model_routing.rank_candidates(board, live, DB.get_success_rates(), latency)


def live_latency_map():
    """Задержка последней успешной пробы по каждой живой текстовой модели."""
    return {(r["provider"], r["model_id"]): r.get("latency_ms", 0) for r in DB.get_live_text_rows()}


def pick_leader():
    """The model an unpinned session answers with. Recomputed from the ranking every time."""
    return model_routing.pick_default(live_model_ranking())


def answer_with_fallback(uid, admin_id, sess, hist, prompt_text, sys_prompt):
    """The one way anyone — the admin included — gets an answer.

    A sandboxed mode is tried first when the session asks for one, and exactly once: any
    failure at all drops through to the native chain with the same history. The sandbox
    used to answer for the admin on its own, so when its CLI could not resolve the model
    the CLI's own "Internal error: There's an issue with the selected model (…[1m])"
    went straight into the chat and nothing else was tried.

    Returns (answer|None, usage, meta, provider, model).
    """
    live = live_pairs()
    current = (sess.get("provider") or PROVIDER_DEFAULT, sess.get("model") or "")
    mode = model_routing.engine_mode_for(
        sess.get("engine_mode"), uid == admin_id,
        claude_model=model_routing.claude_cli_candidate(live, preferred=current))
    if mode != "native":
        agent = acp_agent_for_mode(mode)
        target = harness_target(agent, current[0], sanitize_model_id(current[1]), live=live)
        ans, usage, meta = ask_via_acpx(uid, prompt_text, dict(sess, engine_mode=mode),
                                        sys_prompt=sys_prompt, target=target)
        if ans:
            meta["mode"] = mode
            # Stripped here as well as at the send point: the scratchpad must not reach the
            # chat, and it must not reach the history the next question is asked with.
            return strip_reasoning(ans), usage, meta, target[0], target[1]
        DB.log_request(uid, target[0], target[1], 0, 0, meta.get("finish_reason"), 0,
                       meta.get("error"), mode=mode, request_http_ms=0)
        log.warning(f"{mode} mode failed with {meta.get('error')}; answering natively instead")
    ans, usage, meta, provider, model = native_answer(uid, admin_id, sess, hist, prompt_text, sys_prompt)
    meta["mode"] = "native"
    return ans, usage, meta, provider, model


def native_answer(uid, admin_id, sess, hist, prompt_text, sys_prompt):
    """Ask the ranking in order until something answers.

    Returns (answer|None, usage, meta, provider, model). Every attempt — including the
    failed ones — becomes a request_log row, because the board and the digest see nothing
    that is not in the database.
    """
    ranked = live_model_ranking()
    current = (sess.get("provider") or PROVIDER_DEFAULT, sess.get("model") or "")
    # Only a model the person picked themselves goes first; everyone else gets the leader
    # of the latest measurement, recomputed on every question.
    pinned = current if (sess.get("model_pinned") and current[1]) else None
    chain = model_routing.fallback_chain(ranked, pinned)
    if not chain and current[1]:
        chain = [current]
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    meta = {"finish_reason": None, "tool_calls_total": 0, "error": "no_live_models",
            "http_latency_ms": 0, "rate_limits": {}, "status": None, "retry_after_sec": None}
    provider, model = current
    for provider, model in chain:
        prov = PROVIDERS.get(provider) or PROVIDERS[PROVIDER_DEFAULT]
        api_key = load_provider_key(provider) or load_provider_key(PROVIDER_DEFAULT)
        caps = capabilities_for_model(provider, model)
        use_tools = sess.get("tools_enabled", True) and prov.get("supports_tools", True) and ("tools" in caps)
        messages = [{"role": "system", "content": sys_prompt}] + hist + [{"role": "user", "content": prompt_text}]
        ans, usage, meta = ask_llm(prov["url"], api_key, model, messages, uid=uid, admin_id=admin_id,
                                   use_tools=use_tools, use_proxy=prov.get("proxy", False))
        if ans:
            return ans, usage, meta, provider, model
        DB.log_request(uid, provider, model, usage["prompt_tokens"], usage["completion_tokens"],
                       meta.get("finish_reason"), meta.get("tool_calls_total", 0), meta.get("error"),
                       mode="native", request_http_ms=meta.get("http_latency_ms", 0))
        log.warning(f"fallback: {provider}/{model} failed with {meta.get('error')}, trying the next one")
        if not model_routing.is_retryable(meta.get("status")):
            break
    return None, usage, meta, provider, model


def ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass

ACP_PROTOCOL_LINE = re.compile(
    r"^\s*\[(client|server|agent|done|tool|error|info)\b[^\]]*\].*$", re.MULTILINE)


def strip_acp_noise(text):
    """Drop the agent's protocol chatter — `[client] session/new (running)`, `[done] end_turn`.

    acpx prints its handshake to stdout together with the answer, and all of it used to be
    forwarded to the user verbatim.
    """
    cleaned = ACP_PROTOCOL_LINE.sub("", text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def keep_typing(token, uid):
    """Hold the "typing" indicator for the whole answer.

    Telegram clears it after about five seconds, while answers here average five and
    reach two minutes — so the user sat in silence wondering if the bot was alive.
    Returns a stop function.
    """
    stop = threading.Event()

    def beat():
        # Hard cap: if the caller dies before stopping us, the thread must not outlive the
        # longest possible answer (harness runs are killed at 180s).
        for _ in range(50):
            if stop.wait(4.0):
                return
            try:
                from agent.telegram_api import tg_send_chat_action
                tg_send_chat_action(token, uid, action="typing")
            except Exception:
                return

    threading.Thread(target=beat, daemon=True).start()
    return stop.set


def acp_agent_for_mode(mode):
    m = (mode or "").strip().lower()
    if m in ("claude", "opencode", "pi"):
        return m
    return "claude"


# claude-code and opencode talk the Anthropic Messages protocol, which only OpenRouter answers
# among our providers; pi speaks each provider's native API but only knows these keys.
HARNESS_PROVIDERS = {
    "claude": ("openrouter",),
    "opencode": ("openrouter",),
    "pi": ("openrouter", "groq"),
}

ACPX_APPEND_SYSTEM_PROMPT = "Be concise. Execute directly. Return only essential output and short conclusions."


def live_pairs():
    """(provider, model) the health probe still reaches — the set every mode picks from."""
    now = int(time.time())
    parked = model_routing.parked_providers(DB.get_provider_state(), now)
    return model_routing.live_candidates(DB.get_live_text_rows(), now, parked=parked)


def claude_cli_model_now():
    """The verified pair claude mode would run on right now, or None if there is none."""
    return model_routing.claude_cli_candidate(live_pairs())


def harness_target(agent, provider, model, live=None):
    """(provider, model, switched) — a harness run must not go where it cannot be answered.

    For `claude` the provider is not enough. The CLI needs a model from the verified
    whitelist: the leader of the measurement is picked for answering chat completions,
    and sending it into claude-code produced "There's an issue with the selected model".
    """
    if agent == "claude":
        picked = model_routing.claude_cli_candidate(
            live_pairs() if live is None else live, preferred=(provider, model))
        if picked:
            return picked[0], picked[1], picked != (provider, model)
    if provider in HARNESS_PROVIDERS.get(agent, ("openrouter",)):
        return provider, model, False
    fallback = "openrouter"
    picked = DB.pick_default_text_model(fallback) or PROVIDERS[fallback]["default_model"]
    return fallback, picked, True


def mask_secrets(argv, env):
    """Command line for the log with every key value replaced by its name.

    The acpx line was logged verbatim, so the OpenRouter key sat in plain text in
    journald for anyone with read access to the unit's logs.
    """
    secrets = {str(env.get(name) or ""): name
               for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    secrets.pop("", None)
    out = []
    for part in argv:
        for value, name in secrets.items():
            part = part.replace(value, f"<{name}>")
        out.append(part)
    return out


CLI_ERROR_MARKERS = (
    "internal error:",
    "api error:",
    "issue with the selected model",
    "run --model to pick a different model",
    "rerun with `--verbose`",
)


def looks_like_cli_error(text):
    """True when the sandbox printed a diagnostic where an answer should have been.

    acpx exits 0 while claude-code writes `Internal error: There's an issue with the
    selected model (…)` to stdout, and the exit code alone let that through as the answer.
    """
    head = (text or "").strip().lower()[:400]
    return any(marker in head for marker in CLI_ERROR_MARKERS)


def acpx_failure(uid, finish_reason, error, session_id):
    """A sandbox run that produced no answer. The reason goes to the log, never to the chat."""
    with runtimeStatusLock:
        st = runtimeStatus.get(uid, {})
        st.update({"active": False, "last_error_ts": int(time.time())})
        runtimeStatus[uid] = st
    return None, {"prompt_tokens": 0, "completion_tokens": 0}, {
        "finish_reason": finish_reason, "tool_calls_total": 0, "error": error,
        "session_id": session_id, "http_latency_ms": 0, "rate_limits": {},
        "status": None, "retry_after_sec": None}


def ask_via_acpx(uid, text, sess, sys_prompt="", target=None):
    """Run the question inside the sandbox. Returns (answer|None, usage, meta).

    A failure returns None: the words a person reads are decided by the caller, and the
    CLI's own "Internal error: There's an issue with the selected model (…)" is not one
    of them.
    """
    try:
        mode = sess.get("engine_mode", "native")
        agent = acp_agent_for_mode(mode)
        harness_provider, harness_model, _switched = target or harness_target(
            agent, sess.get("provider", PROVIDER_DEFAULT), sanitize_model_id(sess.get("model") or ""))
        mode_model = harness_model
        sess = dict(sess, provider=harness_provider, model=harness_model)
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
        # The bot's own instructions, not claude-code's: without them the sandbox answered
        # "This is just a casual greeting, not a software engineering task."
        append_prompt = ((sys_prompt + " ") if sys_prompt else "") + ACPX_APPEND_SYSTEM_PROMPT
        env["ACPX_APPEND_SYSTEM_PROMPT"] = append_prompt
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
            # `--model` is not a nicety. Left to the ANTHROPIC_DEFAULT_*_MODEL env vars,
            # claude-code 2.1.138 resolves a 1M-context model to `<id>[1m]` and asks
            # OpenRouter for a model that does not exist; passed explicitly it goes out
            # over ACP session/set_model verbatim.
            run_cmd = podman_base + [
                "acpx", "--cwd", "/workspace", "--format", "text",
                "--approve-all", "--non-interactive-permissions", "deny",
                "--timeout", acpx_timeout,
                "--model", mode_model,
                "--append-system-prompt", append_prompt,
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
        log.info(f"acpx run: {shlex.join(mask_secrets(run_cmd[:-1], env) + ['<task>'])}")
        touch_active()
        lock_wait = float(os.environ.get("BOT_ACPX_LOCK_WAIT", "30") or 30)
        with acpx_lock(timeout=lock_wait, holder=f"user:{uid}") as got_lock:
            if not got_lock:
                with runtimeStatusLock:
                    st = runtimeStatus.get(uid, {})
                    st.update({"active": False, "last_error_ts": int(time.time())})
                    runtimeStatus[uid] = st
                return acpx_failure(uid, "acpx_busy", "lock_busy", session_uuid)
            r = subprocess.run(run_cmd, capture_output=True, text=True, timeout=180, env=env)
        out = strip_acp_noise(r.stdout or "")
        err = (r.stderr or "").strip()
        raw = ((r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")).strip()
        raw_log = f"{user_dir}/.acpx-last-raw.log"
        try:
            Path(raw_log).write_text(raw)
        except Exception:
            pass

        if r.returncode == 0 and out and not looks_like_cli_error(out):
            with runtimeStatusLock:
                st = runtimeStatus.get(uid, {})
                st.update({"active": False, "last_ok_ts": int(time.time())})
                runtimeStatus[uid] = st
            return out, {"prompt_tokens": 0, "completion_tokens": 0}, {"finish_reason": f"acpx_{agent}", "tool_calls_total": 0, "error": None, "session_id": session_uuid}
        msg = err or out or f"acpx prompt failed with exit {r.returncode}"
        log.error(f"acpx failed ({msg[:200]}), raw log: {raw_log}")
        return acpx_failure(uid, "acpx_error", msg[:200], session_uuid)
    except FileNotFoundError:
        return acpx_failure(uid, "acpx_missing", "acpx_missing", locals().get("session_uuid", ""))
    except subprocess.TimeoutExpired:
        return acpx_failure(uid, "acpx_timeout", "timeout", locals().get("session_uuid", ""))
    except Exception as e:
        log.error(f"acpx exception: {e}")
        return acpx_failure(uid, "acpx_exception", str(e)[:200], locals().get("session_uuid", ""))

def send_model_answer(token, uid, text, reply_markup=None):
    """Every word a model produced leaves through here, and nowhere else.

    The three defences against a leaked scratchpad (`reasoning: exclude` in the request,
    ignoring the provider's reasoning fields, cutting `[thinking]` out of the text) were
    wired into the native branch only, so the sandbox answered the admin with
    "[thinking] The user greeted me in Russian…" in front of the actual greeting.
    """
    clean = strip_reasoning(text)
    parsed, ents = parse_markdown_to_entities(normalize_list_markers(clean))
    return tg_send_long_text(token, uid, parsed, entities=ents, reply_markup=reply_markup)


def build_system_prompt(is_admin=False):
    """The sandbox has no network for plain users, so only an admin may be told about the web."""
    if is_admin:
        access = (
            "You are an ADMIN with full internet access. "
            "Environment: Alpine Linux. No 'requests' lib, use 'urllib.request', wget, curl. "
            "Use DuckDuckGo (html.duckduckgo.com) if Google fails. "
        )
    else:
        access = (
            "You are a USER. The sandbox runs with networking disabled: no web search, no downloads, "
            "no API calls. Never offer to look something up online — say you cannot and answer from your "
            "own knowledge. Environment: Alpine Linux, offline. "
        )
    return (
        f"Smolevich AI Bot. Instructions: {access}"
        "Output: Telegram Markdown V2 — use *bold*, _italic_, `inline code`, triple backticks for code "
        "blocks, [text](url) for links. Keep formatting simple and valid for Telegram markdown. "
        "Be concise — show actual command output, no hypothetical examples, no tables with status, "
        "no 'next steps' sections. Just execute and show results. When user sends coordinates "
        "[Геолокация: lat, lon], use them for location-based queries (search nearby places, weather, etc.). "
        "Always complete your answer fully — never cut off mid-sentence."
    )


def build_help_text(sess, is_admin=False):
    is_en = sess.get("ui_lang", "ru") == "en"
    if is_en:
        lines = [
            "Hi. Just write — I'll answer.",
            "",
            "🎙 Send a voice message or an audio file and I'll transcribe it.",
            "Everything else is under ☰. /menu brings the buttons back.",
        ]
        if is_admin:
            lines.append("Admin tools are in /menu -> Admin.")
        return "\n".join(lines)
    lines = [
        "Привет. Просто напиши — отвечу.",
        "",
        "🎙 Пришли голосовое или аудиофайл — расшифрую.",
        "Остальное под ☰. Через /menu кнопки возвращаются.",
    ]
    if is_admin:
        lines.append("Админские инструменты находятся в /menu -> Admin.")
    return "\n".join(lines)

def send_status_text(token, uid, admin_id):
    """Идентификаторы, токены и миллисекунды — только тому, кто их и завёл."""
    if uid != admin_id:
        return
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
    elif provider == "groq":
        rl_text = "n/a (send one request with this provider to populate headers)"
    elif provider == "nvidia":
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
    sess = DB.get_session(uid)
    is_en = sess.get("ui_lang", "ru") == "en"
    if not allow_voice_use(uid, "tts", token, is_en=is_en):
        return
    tg_send_chat_action(token, uid, action="upload_document")
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
        res = tg_send_document_bytes(token, uid, "tts.wav", audio, caption=("🔊 Voiced" if is_en else "🔊 Озвучка"))
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
            # The reason is in media_request_log; the person only needs to know it failed.
            tg_send_text(token, uid, "Не получилось озвучить. Попробуй ещё раз."
                         if not is_en else "Voicing failed. Try again.")
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
        log.error(f"send_tts_audio: {e}")
        tg_send_text(token, uid, "Не получилось озвучить. Попробуй ещё раз."
                     if not is_en else "Voicing failed. Try again.")

def welcome_after_gate(uid, token, admin_id):
    """Passing the gate must open the bot, not end the conversation.

    The gate consumes the user's only /start, so without this they were left staring at
    a one-word confirmation with no keyboard and no menu.
    """
    sess = DB.get_session(uid)
    is_en = sess.get("ui_lang", "ru") == "en"
    tg_request(token, "sendMessage", {
        "chat_id": uid,
        "text": ("You're in. Ask me anything — I pick who answers."
                 if is_en else "Готово. Спрашивай что угодно — кто отвечает, я выберу сам."),
        "reply_markup": build_quick_keyboard(sess),
    })


def handle_callback(cb, token, admin_id):
    uid = cb["from"]["id"]; data = cb.get("data", "")
    log.info(f"Callback from {uid}: {data}")
    # One place catches every inline press, so no screen can be added without telemetry.
    route, _, arg = data.partition(":")
    DB.log_ui_event(uid, route, arg)
    if data.startswith("set_provider:"):
        # The screen behind this button is admin-only; an old keyboard must not be a way
        # around that, and must not answer a person in words they never saw.
        if uid != admin_id:
            say_toast(token, cb["id"], "unavailable", alert=True)
            return
        prov_name = data.split(":", 1)[1]; sess = DB.get_session(uid)
        # An old keyboard may still offer a provider we have since dropped.
        if prov_name not in PROVIDERS:
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": f"❌ Provider {prov_name} is no longer available.", "show_alert": True})
            return
        default_model = DB.pick_default_text_model(prov_name) or PROVIDERS[prov_name]["default_model"]
        default_tools = PROVIDERS[prov_name].get("supports_tools", True)
        DB.save_session(uid, default_model, sess["history"], provider=prov_name, tools_enabled=default_tools, engine_mode=sess.get("engine_mode", "native"), model_pinned=True)
        tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": f"💬 Теперь отвечает {model_routing.human_model_name(default_model)}. Спрашивай.", "reply_markup": {"inline_keyboard": [[{"text": "← Назад", "callback_data": "menu:back"}]]}})
    elif data.startswith("try:"):
        # Straight from a leaderboard row: switch provider and model together, stay on the list.
        _, code, model = data.split(":", 2)
        prov_name = PROVIDER_BY_CODE.get(code, "")
        sess = DB.get_session(uid)
        is_en = sess.get("ui_lang", "ru") == "en"
        if prov_name not in PROVIDERS:
            say_toast(token, cb["id"], "unavailable", is_en, alert=True)
            return
        model = sanitize_model_id(model)
        # Chosen by hand: pin it, or the next question would silently go to the leader.
        DB.save_session(uid, model, sess["history"], provider=prov_name,
                        tools_enabled=PROVIDERS[prov_name].get("supports_tools", True),
                        engine_mode=sess.get("engine_mode", "native"), model_pinned=True)
        short = model_routing.human_model_name(model)
        tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"],
                                                  "text": (f"Answering with {short}" if is_en else f"Теперь отвечает {short}")})
        tg_send_text(token, uid, (f"💬 {short} answers now. Ask anything."
                                  if is_en else f"💬 Теперь отвечает {short}. Спрашивай."))
    elif data.startswith("set_model:"):
        m = sanitize_model_id(data.split(":", 1)[1])
        sess = DB.get_session(uid)
        info = DB.get_model_info(sess["provider"], m)
        # Reject non-chat categories: audio/image/video/embedding/safety/translation are
        # used via /stt /tts /video flows. Only block when category is known and non-chat —
        # missing health-check rows shouldn't prevent picking a fresh model.
        cat = (info or {}).get("category") or ""
        if cat and cat not in ("text", "code"):
            say_toast(token, cb["id"], "not_for_chat", sess.get("ui_lang", "ru") == "en", alert=True)
            return
        DB.save_session(uid, m, sess["history"], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode=sess.get("engine_mode", "native"), model_pinned=True)
        is_en = sess.get("ui_lang", "ru") == "en"
        short = model_routing.human_model_name(m)
        # Latency, category and "supports tools" are our plumbing; a person needs to know
        # who answers now and how to get back.
        txt = (f"💬 {short} is answering. Ask away." if is_en else f"💬 Отвечает {short}. Спрашивай.")
        back_kb = {"inline_keyboard": [[{"text": ("← Back" if is_en else "← Назад"), "callback_data": "menu:back"}]]}
        res = tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": txt, "reply_markup": back_kb})
        # Telegram returns "message is not modified" when user taps the already selected model.
        if not res.get("ok"):
            desc = (res.get("description") or "").lower()
            if "message is not modified" in desc:
                say_toast(token, cb["id"], "already", is_en)
            else:
                say_toast(token, cb["id"], "failed", is_en, alert=True)
        else:
            say_toast(token, cb["id"], "done", is_en)
    elif data in ("check_sub", "request_access"):
        # Old gate messages still sit in people's chats. There is nothing to check now.
        DB.set_allowed(uid, True)
        tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": "✅"})
        welcome_after_gate(uid, token, admin_id)
    elif data.startswith("set_debug:"):
        if uid != admin_id:
            say_toast(token, cb["id"], "unavailable", alert=True)
            return
        mode = data.split(":", 1)[1]
        with DEBUG_USERS_LOCK:
            if mode == "on": DEBUG_USERS.add(uid)
            elif uid in DEBUG_USERS: DEBUG_USERS.remove(uid)
        tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": f"✅ Debug footer: {mode.upper()}"})
    elif data.startswith("set_mode:"):
        if uid != admin_id:
            say_toast(token, cb["id"], "unavailable", alert=True)
            return
        mode = data.split(":", 1)[1]
        sess = DB.get_session(uid)
        if mode == "claude" and not claude_cli_model_now():
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Ни одна проверенная модель сейчас не отвечает через claude CLI.", "show_alert": True})
            return
        DB.save_session(uid, sess["model"], sess["history"], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode=mode)
        tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": f"✅ Mode: {mode}"})
    elif data.startswith("set_tools:"):
        if uid != admin_id:
            say_toast(token, cb["id"], "unavailable", alert=True)
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
        # A reset returns the engine to native too. A session pinned to an agent mode kept
        # answering through it after every reset, so the admin's own row sat in `claude`
        # for weeks and nobody remembered choosing it.
        DB.save_session(uid, sess["model"], [], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode="native")
        DB.set_last_session_id(uid, "")
        with runtimeStatusLock:
            runtimeStatus.pop(uid, None)
        u_dir = os.path.join(SESSIONS_ROOT, str(uid))
        if os.path.exists(u_dir): shutil.rmtree(u_dir); os.makedirs(u_dir)
        tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": cb["message"].get("text", "") + "\n\n✅ Context reset done."})
        say_toast(token, cb["id"], "reset")
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
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
        elif action == "curious":
            c_txt, c_kb = build_curious_view(sess, is_admin=(uid == admin_id))
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": c_txt, "reply_markup": {"inline_keyboard": c_kb}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
        elif action == "settings":
            s_txt, s_kb = build_menu_settings(sess, is_admin=(uid == admin_id))
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": s_txt, "reply_markup": {"inline_keyboard": s_kb}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
        elif action == "admin":
            if uid != admin_id:
                say_toast(token, cb["id"], "unavailable", alert=True)
                return
            a_txt, a_kb = build_admin_menu(sess)
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": a_txt, "reply_markup": {"inline_keyboard": a_kb}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
        elif action == "chat":
            use_provider, use_model, switched = ensure_text_model_for_session(sess)
            DB.save_session(uid, use_model, sess["history"], provider=use_provider, tools_enabled=sess["tools_enabled"], engine_mode="native")
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": ("💬 Chat mode enabled.\nSend text and I'll reply." if is_en else "💬 Чат-режим включён.\nПиши обычный текст — я отвечу."), "reply_markup": {"inline_keyboard": [[{"text": back_label, "callback_data": "menu:back"}]]}})
            say_toast(token, cb["id"], "chat", is_en)
        elif action == "code":
            if uid != admin_id:
                say_toast(token, cb["id"], "unavailable", alert=True)
                return
            # This button is what put the admin's row into `claude` in the first place, and
            # it handed the sandbox whatever model the measurement liked that day. The CLI
            # needs a model from the verified list, so without one the mode stays off.
            claude_pair = claude_cli_model_now()
            if not claude_pair:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Ни одна проверенная модель сейчас не отвечает через claude CLI.", "show_alert": True})
                return
            use_provider, use_model, switched = claude_pair[0], claude_pair[1], claude_pair != (sess.get("provider"), sess.get("model"))
            DB.save_session(uid, use_model, sess["history"], provider=use_provider, tools_enabled=sess["tools_enabled"], engine_mode="claude")
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": ("🛠 Code mode (Claude Code) enabled.\nSend a task and I'll run it in sandbox." if is_en else "🛠 Код-режим (Claude Code) включён.\nОтправь задачу — выполню в песочнице."), "reply_markup": {"inline_keyboard": [[{"text": back_label, "callback_data": "menu:back"}]]}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": ("Switched to text model" if switched and is_en else ("Переключил на текстовую модель" if switched else "Код"))})
        elif action in ("voice", "stt"):
            if not has_stt_models():
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": unavailable_message("stt", feature_retry_after_sec("stt"), is_en), "show_alert": True})
                return
            with pendingSttUsersLock:
                pendingSttUsers.add(uid)
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": ("🎙 Speech-to-text enabled.\nSend a voice/audio message." if is_en else "🎙 Речь в текст включена.\nПришли голосовое или аудиофайл."), "reply_markup": {"inline_keyboard": [[{"text": back_label, "callback_data": "menu:back"}]]}})
            say_toast(token, cb["id"], "stt", is_en)
        elif action == "tts":
            if not has_tts_models():
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": unavailable_message("tts", feature_retry_after_sec("tts"), is_en), "show_alert": True})
                return
            with pendingTtsUsersLock:
                pendingTtsUsers.add(uid)
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": ("🔊 Text-to-speech enabled.\nSend text and I will return audio." if is_en else "🔊 Текст в речь включён.\nПришли текст — верну аудио."), "reply_markup": {"inline_keyboard": [[{"text": back_label, "callback_data": "menu:back"}]]}})
            say_toast(token, cb["id"], "tts", is_en)
        elif action == "video":
            _, video_model = pick_video_detector()
            if not video_model:
                tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": unavailable_message("video", feature_retry_after_sec("video"), is_en), "show_alert": True})
                return
            with pendingVideoUsersLock:
                pendingVideoUsers.add(uid)
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": ("🕵️ Send an MP4/WebM file, up to 20 MB — I'll tell whether it looks AI-made." if is_en else "🕵️ Пришли файл MP4/WebM до 20 МБ — скажу, похоже ли на сгенерированное."), "reply_markup": {"inline_keyboard": [[{"text": back_label, "callback_data": "menu:back"}]]}})
            say_toast(token, cb["id"], "video", is_en)
        elif action == "lang_toggle":
            new_lang = "en" if sess.get("ui_lang", "ru") == "ru" else "ru"
            DB.save_session(uid, sess["model"], sess["history"], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode=sess.get("engine_mode", "native"), ui_lang=new_lang)
            sess["ui_lang"] = new_lang
            s_txt, s_kb = build_menu_settings(sess, is_admin=(uid == admin_id))
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": s_txt, "reply_markup": {"inline_keyboard": s_kb}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": ("Language: " if new_lang == "en" else "Язык: ") + ("EN" if new_lang == "en" else "RU")})

        elif action == "model":
            # Экран остался только у админа; у человека кнопка живёт в старых
            # клавиатурах, и вести ей некуда, кроме той же пятёрки.
            if uid == admin_id:
                m_txt, m_kb = build_models_view(sess, category="text", limit=12, is_admin=True)
            else:
                m_txt, m_kb = build_curious_view(sess)
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": m_txt, "reply_markup": {"inline_keyboard": m_kb}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
        elif action == "provider":
            if uid != admin_id:
                say_toast(token, cb["id"], "unavailable", alert=True)
                return
            avail = available_providers()
            kb = [[{"text": f"{'✅ ' if name == sess['provider'] else ''}{provider_display_name(name)}", "callback_data": f"set_provider:{name}"}] for name in avail]
            # Every other screen ends with the same row; this one did not, and the only
            # way out was to close the menu and start over.
            kb.append([{"text": back_label, "callback_data": "menu:model"}])
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": ("Where answers come from" if is_en else "Откуда брать ответы"), "reply_markup": {"inline_keyboard": kb}})
        elif action == "reset":
            DB.save_session(uid, sess["model"], [], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode="native")
            DB.set_last_session_id(uid, "")
            with runtimeStatusLock:
                runtimeStatus.pop(uid, None)
            u_dir = os.path.join(SESSIONS_ROOT, str(uid))
            if os.path.exists(u_dir): shutil.rmtree(u_dir); os.makedirs(u_dir)
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": ("✅ History reset." if is_en else "✅ История сброшена."), "reply_markup": {"inline_keyboard": [[{"text": back_label, "callback_data": "menu:back"}]]}})
            say_toast(token, cb["id"], "reset", is_en)
        elif action == "status":
            if uid != admin_id:
                say_toast(token, cb["id"], "unavailable", alert=True)
                return
            send_status_text(token, uid, admin_id)
            tg_send_text(token, uid, build_provider_health_text())
            tg_send_text(token, uid, build_board_admin_text())
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Статус отправлен"})
        elif action == "help":
            tg_request(token, "sendMessage", {"chat_id": uid, "text": build_help_text(sess, is_admin=(uid == admin_id))})
            say_toast(token, cb["id"], "help_sent", is_en)
        elif action == "top":
            # Old keyboards still carry this route; it lands on the screen that replaced it.
            c_txt, c_kb = build_curious_view(sess, is_admin=(uid == admin_id))
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": c_txt, "reply_markup": {"inline_keyboard": c_kb}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
        elif action == "mode":
            if uid != admin_id:
                say_toast(token, cb["id"], "unavailable", alert=True)
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
                say_toast(token, cb["id"], "unavailable", alert=True)
                return
            kb = [[{"text": f"{'✅ ' if sess.get('tools_enabled', True) else ''}On", "callback_data": "set_tools:on"},
                   {"text": f"{'✅ ' if not sess.get('tools_enabled', True) else ''}Off", "callback_data": "set_tools:off"}]]
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "Tools usage:", "reply_markup": {"inline_keyboard": kb}})
        elif action == "debug":
            if uid != admin_id:
                say_toast(token, cb["id"], "unavailable", alert=True)
                return
            with DEBUG_USERS_LOCK:
                is_on = uid in DEBUG_USERS
            kb = [[{"text": f"{'✅ ' if is_on else ''}On", "callback_data": "set_debug:on"},
                   {"text": f"{'✅ ' if not is_on else ''}Off", "callback_data": "set_debug:off"}]]
            tg_request(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "Debug footer settings:", "reply_markup": {"inline_keyboard": kb}})
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Debug отправлен"})
        elif action == "users":
            if uid != admin_id:
                say_toast(token, cb["id"], "unavailable", alert=True)
                return
            send_users_text(token, uid, admin_id)
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Users отправлены"})
        else:
            # Экран мог исчезнуть, а кнопка на него — остаться в чьей-то переписке.
            # Ветки нет, но спиннер обязан погаснуть.
            tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
    else:
        tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})

def take_pending_tts(uid):
    """Consume the one-shot TTS flag: True means this text must come back as audio."""
    with pendingTtsUsersLock:
        if uid in pendingTtsUsers:
            pendingTtsUsers.discard(uid)
            return True
    return False


def handle_quick_action(action, uid, token, admin_id, message_id=None):
    """Bottom-keyboard buttons arrive as plain text, not as callbacks.

    Telegram has no way to press a reply-keyboard button without sending its label, so
    the tap is deleted the moment it is understood — otherwise "☰ Ещё" stands in the
    chat as if the person had typed it.
    """
    DB.log_ui_event(uid, "quick", action)
    if message_id:
        tg_request(token, "deleteMessage", {"chat_id": uid, "message_id": message_id})
    sess = DB.get_session(uid)
    is_en = sess.get("ui_lang", "ru") == "en"
    if action == "chat":
        # Leaving a voice mode has to be possible, or the next message never reaches the model.
        with pendingSttUsersLock:
            pendingSttUsers.discard(uid)
        with pendingTtsUsersLock:
            pendingTtsUsers.discard(uid)
        tg_send_text(token, uid, "💬 Ask anything — I'll answer." if is_en else "💬 Пиши вопрос — отвечу.")
    elif action == "stt":
        if not has_stt_models():
            tg_send_text(token, uid, unavailable_message("stt", feature_retry_after_sec("stt"), is_en))
            return True
        with pendingSttUsersLock:
            pendingSttUsers.add(uid)
        tg_send_text(token, uid, "🎙 Send a voice message or an audio file." if is_en else "🎙 Пришли голосовое или аудиофайл.")
    elif action == "tts":
        if not has_tts_models():
            tg_send_text(token, uid, unavailable_message("tts", feature_retry_after_sec("tts"), is_en))
            return True
        with pendingTtsUsersLock:
            pendingTtsUsers.add(uid)
        tg_send_text(token, uid, "🔊 Send the text to voice." if is_en else "🔊 Пришли текст — верну аудио.")
    elif action in ("board", "model"):
        # Both labels are gone from the layout but live on in clients that still show the
        # old keyboard. Answer what they asked for, and replace the stale layout.
        tg_request(token, "sendMessage", {
            "chat_id": uid,
            "text": ("Updated the buttons — this now lives under ☰." if is_en
                     else "Обновил кнопки — это теперь под ☰."),
            "reply_markup": build_quick_keyboard(sess),
        })
        c_txt, c_kb = build_curious_view(sess, is_admin=(uid == admin_id))
        tg_request(token, "sendMessage", {"chat_id": uid, "text": c_txt, "reply_markup": {"inline_keyboard": c_kb}})
    elif action == "more":
        m_txt, m_kb = build_menu_root(sess, is_admin=(uid == admin_id))
        tg_request(token, "sendMessage", {"chat_id": uid, "text": m_txt, "reply_markup": {"inline_keyboard": m_kb}})
    return True


def handle_command(uid, username, text, token, admin_id):
    cmd = text.split(maxsplit=1)[0].lower()
    DB.log_ui_event(uid, "command", cmd)
    sess = DB.get_session(uid)
    if cmd == "/menu" or cmd == "/start":
        sess = DB.get_session(uid)
        is_en = sess.get("ui_lang", "ru") == "en"
        # A reply keyboard and an inline one cannot share a message, so the bottom row comes first.
        tg_request(token, "sendMessage", {
            "chat_id": uid,
            "text": ("💬 Ask anything — I'll answer." if is_en else "💬 Пиши вопрос — отвечу."),
            "reply_markup": build_quick_keyboard(sess),
        })
        m_txt, m_kb = build_menu_root(sess, is_admin=(uid == admin_id))
        tg_request(token, "sendMessage", {"chat_id": uid, "text": m_txt, "reply_markup": {"inline_keyboard": m_kb}})
    elif cmd == "/help":
        tg_request(token, "sendMessage", {"chat_id": uid, "text": build_help_text(sess, is_admin=(uid == admin_id))})
    elif cmd == "/feedback":
        is_en = sess.get("ui_lang", "ru") == "en"
        body = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        if not body:
            tg_send_text(token, uid, "Write it as: /feedback your message." if is_en else "Напиши так: /feedback и текст сообщения.")
            return True
        tg_send_text(token, admin_id, f"📝 Отзыв от {username} ({uid}):\n\n{body}")
        tg_send_text(token, uid, "Sent. Thank you." if is_en else "Отправил. Спасибо.")
    else:
        tg_send_text(token, uid, "Такой команды нет — всё есть в кнопках снизу.")
    return True


LEADERBOARD_URL = "https://notes-share.smolevich90.workers.dev/api/smolevich-ai-bot/free-models"
LEADERBOARD_CACHE_TTL_SEC = 300
PROVIDER_CODES = {"openrouter": "o", "groq": "g", "nvidia": "n"}
PROVIDER_BY_CODE = {v: k for k, v in PROVIDER_CODES.items()}

leaderboardCache = {"ts": 0.0, "payload": None}
leaderboardCacheLock = threading.Lock()


def fetch_leaderboard(force=False):
    """The board the benchmark publishes. Cached: a button press must not hit the worker."""
    with leaderboardCacheLock:
        cached = leaderboardCache["payload"]
        if cached is not None and not force and time.time() - leaderboardCache["ts"] < LEADERBOARD_CACHE_TTL_SEC:
            return cached
    try:
        req = urllib.request.Request(LEADERBOARD_URL, headers={"User-Agent": "smolevich-ai-bot"})
        with urllib.request.urlopen(req, timeout=10) as f:
            payload = json.loads(f.read().decode())
    except Exception as e:
        log.error(f"leaderboard fetch: {e}")
        with leaderboardCacheLock:
            return leaderboardCache["payload"]
    with leaderboardCacheLock:
        leaderboardCache["ts"] = time.time()
        leaderboardCache["payload"] = payload
    return payload


def solved_out_of_ten(entry):
    """Answers solved out of ten; None when the model has not been measured enough."""
    if entry.get("provisional"):
        return None
    direct = entry.get("solved_of_ten")
    if direct is not None:
        try:
            return int(direct)
        except (TypeError, ValueError):
            return None
    native = (entry.get("scores") or {}).get("native")
    if native is None:
        return None
    try:
        return int(round(float(native) * 10))
    except (TypeError, ValueError):
        return None


CURIOUS_ROWS = 5


BADGE_LABELS = {
    "steadiest": {"ru": "✔ самая стабильная", "en": "✔ the steadiest"},
    "fastest": {"ru": "⚡ быстрая", "en": "⚡ the fastest"},
}


def build_curious_view(sess, is_admin=False):
    """The measurement, for whoever wants to look. Nobody has to.

    A ten-row table of `minimax-m3:free · OpenRouter · решает 8 из 10` was the second
    screen an ordinary person saw, and it asked them to do the bot's job. What is left
    here is five names with the badges the measurement supports, and a way back.

    «🤖 Выбрать вручную» вело на вторую витрину тех же моделей — двенадцать имён без
    порядка и без пометок. Для человека её больше нет: выбирать не из чего, если
    пятёрка уже отсортирована. Админу список нужен и остаётся.
    """
    is_en = sess.get("ui_lang", "ru") == "en"
    back = [{"text": ("← Back" if is_en else "← Назад"), "callback_data": "menu:back"}]
    manual = [{"text": ("🤖 Choose by hand" if is_en else "🤖 Выбрать вручную"), "callback_data": "menu:model"}]
    tail = ([manual, back] if is_admin else [back])
    ranked = live_model_ranking()[:CURIOUS_ROWS]
    if not ranked:
        txt = ("Still measuring — everything answers as usual meanwhile."
               if is_en else "Ещё измеряю — на вопросы это никак не влияет.")
        return txt, tail

    badges = model_routing.badge_keys(ranked, live_latency_map())
    lines = ["Who answers best right now" if is_en else "Кто сейчас отвечает лучше всех"]
    kb = []
    for candidate in ranked:
        provider, model = candidate
        name = model_routing.human_model_name(model)
        key = badges.get(candidate)
        badge = f"  {BADGE_LABELS[key]['en' if is_en else 'ru']}" if key else ""
        lines.append(f"• {name}{badge}")
        code = PROVIDER_CODES.get(provider)
        if code:
            kb.append([{"text": name[:60], "callback_data": f"try:{code}:{model}"}])
    lines.append("\nYou don't have to choose — I use the top one." if is_en
                 else "\nВыбирать не обязательно — сам беру верхнюю.")
    return "\n".join(lines), kb + tail


def build_board_admin_text():
    """The raw measurement, with the numbers the user screen no longer shows."""
    payload = fetch_leaderboard()
    entries = (payload or {}).get("models") or []
    if not entries:
        return "Борд пуст — замеры ещё идут."
    entries = sorted(entries, key=lambda e: (solved_out_of_ten(e) is None, -(solved_out_of_ten(e) or 0)))
    lines = ["🏆 Борд (замер)"]
    for i, e in enumerate(entries, start=1):
        solved = solved_out_of_ten(e)
        verdict = "данных мало" if solved is None else f"решает {solved} из 10"
        lines.append(f"{i}. {e.get('model') or ''} · {(e.get('provider') or '').strip()} — {verdict}")
    updated = (payload or {}).get("updatedAt") or ""
    if updated[:10]:
        lines.append(f"\nЗамерено {updated[:10]}")
    return "\n".join(lines)


def build_provider_health_text():
    """Admin-facing: a provider whose models are all dead reads as 0/N here.

    HuggingFace sat at 0/130 for three months because nothing ever surfaced it.
    """
    rows = DB.get_provider_health()
    if not rows:
        return "Нет данных о провайдерах."
    now = int(time.time())
    lines = ["🩺 Провайдеры (живых/всего)"]
    for r in rows:
        icon = "🔴" if not r["live"] else ("🟡" if r["live"] * 4 < r["total"] else "🟢")
        ago = now - int(r["last_check"] or 0)
        checked = f"{ago // 60}м назад" if ago < 3600 else f"{ago // 3600}ч назад"
        lines.append(f"{icon} {r['provider']}: {r['live']}/{r['total']} — проверка {checked}")
    dead = [r["provider"] for r in rows if not r["live"]]
    if dead:
        lines.append(f"\n⚠️ Полностью мёртвые: {', '.join(dead)}. Проверь ключ и биллинг.")
    for s in DB.get_provider_state():
        until = int(s.get("disabled_until") or 0)
        reason = str(s.get("reason") or "")[:90]
        if until == PROVIDER_PARKED_UNTIL_HUMAN:
            lines.append(f"\n⛔️ {s['provider']} выключен до ручного включения: {reason}")
        elif until > now:
            left_h = max(0, (until - now) // 3600)
            lines.append(f"\n⛔️ {s['provider']} снят с обстрела ещё на {left_h} ч: {reason}")
    return "\n".join(lines)


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

def ensure_access(uid, username, token, admin_id):
    """Everyone is in. The row is still written — the fleet digest counts people here.

    The channel-subscription gate is gone: it was where most first-timers stopped, and
    what it protected costs nothing per text question. What does cost per call —
    transcription and voicing — is capped by the hour instead, in `allow_voice_use`.
    """
    if not DB.update_and_check(uid, username) and uid != admin_id:
        DB.set_allowed(uid, True)
    return True


def allow_voice_use(uid, kind, token, is_en=False):
    """True if this transcription/voicing fits under the hourly ceiling; else say when."""
    now = int(time.time())
    allowed, retry_at = quota.voice_quota(DB.count_voice_uses(uid, kind), now)
    if not allowed:
        DB.log_ui_event(uid, "quota", kind)
        tg_send_text(token, uid, quota.voice_limit_message(retry_at, now, kind=kind, is_en=is_en))
        return False
    DB.log_voice_use(uid, kind)
    return True


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
        username = fi.get("username") or f"{fi.get('first_name', '')} {fi.get('last_name', '')}".strip()
        # Access is checked here, above every media branch: voice and video used to slip past it.
        if not ensure_access(uid, username, token, admin_id):
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
                # The detector lives outside the session: picking it must not replace the user's LLM.
                provider, selected_model = pick_video_detector()
                if not selected_model:
                    is_en = sess_for_media.get("ui_lang", "ru") == "en"
                    tg_send_text(token, uid, unavailable_message("video", feature_retry_after_sec("video"), is_en))
                    return
                model_info = DB.get_model_info(provider, selected_model)
                if model_info and not model_info.get("available", False):
                    tg_send_text(token, uid, unavailable_message(
                        "video", feature_retry_after_sec("video"),
                        sess_for_media.get("ui_lang", "ru") == "en"))
                    return
                media = msg.get("video") or msg.get("animation") or msg.get("document")
                mime_type = str((media or {}).get("mime_type", "")).lower()
                file_name = str((media or {}).get("file_name", "")).lower()
                if mime_type == "image/gif" or file_name.endswith(".gif"):
                    is_en = sess_for_media.get("ui_lang", "ru") == "en"
                    tg_send_text(token, uid, media_too_big_or_wrong_format("video_format", is_en=is_en))
                    return
                media_size = int(media.get("file_size") or 0)
                if media_size > TELEGRAM_BOT_FILE_DOWNLOAD_LIMIT_BYTES:
                    is_en = sess_for_media.get("ui_lang", "ru") == "en"
                    limit = format_bytes(TELEGRAM_BOT_FILE_DOWNLOAD_LIMIT_BYTES, is_en)
                    got = format_bytes(media_size, is_en)
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
                    tg_send_text(token, uid, media_too_big_or_wrong_format(
                        "video_size", got=got, limit=limit, is_en=is_en))
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
                    send_model_answer(token, uid, format_video_analysis(analysis, lang=lang, caption_text=caption_text))
                else:
                    tg_send_text(token, uid, "Не получилось разобрать это видео. Попробуй другое."
                                 if lang != "en" else "Could not read this video. Try another one.")
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
                tg_send_text(token, uid, "Не получилось проверить видео. Попробуй ещё раз."
                             if sess_for_media.get("ui_lang", "ru") != "en" else "Video check failed. Try again.")
            finally:
                with pendingVideoUsersLock:
                    pendingVideoUsers.discard(uid)
            return

        if (stt_pending or auto_stt_model) and ("voice" in msg or "audio" in msg or "document" in msg):
            if not allow_voice_use(uid, "stt", token, is_en=sess_for_media.get("ui_lang", "ru") == "en"):
                return
            try:
                media = msg.get("voice") or msg.get("audio") or msg.get("document")
                media_size = int(media.get("file_size") or 0)
                if media_size > TELEGRAM_BOT_FILE_DOWNLOAD_LIMIT_BYTES:
                    is_en = sess_for_media.get("ui_lang", "ru") == "en"
                    limit = format_bytes(TELEGRAM_BOT_FILE_DOWNLOAD_LIMIT_BYTES, is_en)
                    got = format_bytes(media_size, is_en)
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
                    tg_send_text(token, uid, media_too_big_or_wrong_format(
                        "audio_size", got=got, limit=limit, is_en=is_en))
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
                    is_en = sess_for_media.get("ui_lang", "ru") == "en"
                    tg_send_long_text(token, uid, ("📝 Transcript:\n" if is_en else "📝 Расшифровка:\n") + transcript)
                else:
                    tg_send_text(token, uid, "Слов в записи не разобрал. Попробуй записать ещё раз."
                                 if sess_for_media.get("ui_lang", "ru") != "en" else "No words came out of this recording. Try again.")
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
                tg_send_text(token, uid, "Не получилось расшифровать. Попробуй ещё раз."
                             if sess_for_media.get("ui_lang", "ru") != "en" else "Transcription failed. Try again.")
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
        elif "photo" in msg or "sticker" in msg:
            # Silence read as "the bot is broken"; there is no image model to route these to.
            is_en = DB.get_session(uid).get("ui_lang", "ru") == "en"
            tg_send_text(token, uid, "🖼 I can't read images yet. Describe it and I'll answer."
                         if is_en else "🖼 Картинки я пока не разбираю. Опиши словами — отвечу.")
            return
        else:
            return
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

        if text.startswith("/"): return handle_command(uid, username, text, token, admin_id)

        quick = quick_action_for(text)
        if quick:
            return handle_quick_action(quick, uid, token, admin_id, message_id=msg.get("message_id"))

        if take_pending_tts(uid):
            send_tts_audio(token, uid, text)
            return True

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
                    tg_send_text(token, uid, "⏳ Ещё отвечаю на прошлое сообщение. Это сохранил — отвечу следом.")
                    inflightBusyNoticeTs[uid] = now_ts
                return
            inflightUsers.add(uid)

        sess = DB.get_session(uid); hist = sess["history"]; model = sess["model"]; provider = sess["provider"]
        fixed_provider, fixed_model, switched_to_text = ensure_text_model_for_session(sess)
        if switched_to_text:
            provider = fixed_provider
            model = fixed_model
            sess["provider"] = provider
            sess["model"] = model
            DB.save_session(
                uid,
                model,
                hist,
                provider=provider,
                tools_enabled=sess["tools_enabled"],
                engine_mode=sess.get("engine_mode", "native"),
                ui_lang=sess.get("ui_lang", "ru"),
            )
        prov = PROVIDERS.get(provider, PROVIDERS[PROVIDER_DEFAULT])
        api_key = load_provider_key(provider) or load_provider_key(PROVIDER_DEFAULT)
        use_proxy = prov.get("proxy", False)

        if estimate_tokens(hist) > MAX_CONTEXT_TOKENS:
            hist = compact_history(prov["url"], api_key, model, hist, uid, admin_id, use_proxy=use_proxy)

        sys_prompt = build_system_prompt(is_admin=(uid == admin_id))

        from agent.telegram_api import tg_send_chat_action
        tg_send_chat_action(token, uid, action="typing")
        stop_typing = keep_typing(token, uid)
        # One door for everybody. Choosing the engine, running the sandbox and falling back
        # to the native chain all happen inside; nothing here knows which one answered.
        ans, usage, meta, provider, model = answer_with_fallback(uid, admin_id, sess, hist, text, sys_prompt)
        mode = meta.get("mode", "native")
        if not ans:
            # Every candidate refused. This is the only moment the person hears about it,
            # and they hear it without model ids, provider names or HTTP codes.
            stop_typing()
            with inflightUsersLock:
                inflightUsers.discard(uid)
                inflightBusyNoticeTs.pop(uid, None)
            tg_send_text(token, uid, model_routing.all_failed_message(
                is_en=sess.get("ui_lang", "ru") == "en",
                retry_after_sec=meta.get("retry_after_sec")))
            return
        DB.add_usage(uid, usage['prompt_tokens'], usage['completion_tokens'])
        req_id = DB.log_request(uid, provider, model, usage['prompt_tokens'], usage['completion_tokens'],
                                meta['finish_reason'], meta['tool_calls_total'], meta['error'], mode=mode, request_http_ms=meta.get("http_latency_ms", 0))
        with runtimeStatusLock:
            st_now = dict(runtimeStatus.get(uid, {}))
            st_now["last_rate_limits"] = meta.get("rate_limits", {}) or {}
            st_now["last_rate_limits_provider"] = provider
            st_now["last_rate_limits_ts"] = int(time.time())
            runtimeStatus[uid] = st_now
        hist.append({"role": "user", "content": text}); hist.append({"role": "assistant", "content": ans})
        # Avoid clobbering mode/tools with stale in-memory session when updates are processed concurrently.
        latest = DB.get_session(uid)
        # A chosen model survives a fallback: the person keeps it for the next question.
        stored_provider = sess.get("provider") if latest.get("model_pinned") else provider
        stored_model = sess.get("model") if latest.get("model_pinned") else model
        DB.save_session(
            uid,
            stored_model,
            hist,
            provider=stored_provider,
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
        if should_show_debug_footer(uid, admin_id):
            footer = f"[{provider}/{model_short} | In: {usage['prompt_tokens']} | Out: {usage['completion_tokens']} | Ctx: {estimate_tokens(hist)}/{MAX_CONTEXT_TOKENS}{sid_part}]"
            raw_reply += f"\n\n_{footer}_"
            
        kb = None
        if estimate_tokens(hist) > MAX_CONTEXT_TOKENS * 0.5:
            kb = {"inline_keyboard": [[{"text": "🔄 Reset Context", "callback_data": "reset_context"}]]}
            
        send_res = send_model_answer(token, uid, raw_reply, reply_markup=kb)
        DB.set_request_delivered(req_id, bool(send_res.get("ok")))
        queued = None
        with pendingTextByUserLock:
            queued = pendingTextByUser.pop(uid, None)
        stop_typing()
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
