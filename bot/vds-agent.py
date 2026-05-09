#!/usr/bin/env python3
"""Telegram bot with SQLite, Podman Sandboxing, Token Tracking, Feedback, Persistent Workspaces, and Debug Logging."""
import json
import logging
import urllib.request
import threading
import sys
import time
import os
import shutil
import subprocess
import urllib.error
import shlex
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid
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
    to_telegram_markdown,
)
from agent.provider_api import available_providers, fetch_models, load_provider_key, make_opener
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
executorPool = ThreadPoolExecutor(max_workers=10)
runtimeStatus = {}
runtimeStatusLock = threading.Lock()
pendingSttUsers = set()
pendingSttUsersLock = threading.Lock()

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
    if any(k in mid for k in ("coder", "codestral", "devstral", "starcoder")):
        return "code"
    return "text"


def capabilities_for_model(provider, model_id):
    model = (model_id or "").lower()
    caps = []
    info = DB.get_model_info(provider, model_id)
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
    if category == "image" or any(k in model for k in ("image", "sdxl", "flux", "stable-diffusion")):
        caps.append("image")
    if category == "video" or "video" in model:
        caps.append("video")
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

def build_models_view(sess, category="text", limit=12):
    prov = sess["provider"]
    category = (category or "text").lower()
    if category not in ("text", "audio"):
        category = "text"
    ms = DB.get_recent_models(prov, max_age_sec=600, category=category, limit=limit)
    if not ms:
        fresh = fetch_models(prov, log)
        ms = []
        for m in fresh:
            mid = m["id"]
            cat = categorize_model_local(mid)
            if category == "audio" and cat != "audio":
                continue
            if category == "text" and cat == "audio":
                continue
            ms.append({
                "id": mid,
                "latency_ms": 0,
                "available": True,
                "supportsTools": bool(m.get("supportsTools")),
            })
            if len(ms) >= limit:
                break
    kb = []
    toggle_row = [
        {"text": f"{'✅ ' if category == 'text' else ''}Text", "callback_data": "models_cat:text"},
        {"text": f"{'✅ ' if category == 'audio' else ''}Audio", "callback_data": "models_cat:audio"},
    ]
    kb.append(toggle_row)
    for m in ms[:limit]:
        mid = m["id"]
        latency = m.get("latency_ms") or 0
        available = m.get("available", True)
        tools_icon = "🛠" if m.get("supportsTools") else ""
        if available and latency:
            status_icon = "🟢"
        elif available:
            status_icon = "⚪"
        else:
            status_icon = "🔴"
        latency_tag = f" {latency}ms" if latency else ""
        label = mid.split("/")[-1] if "/" in mid else mid
        kb.append([{"text": f"{status_icon}{tools_icon} {label}{latency_tag}", "callback_data": f"set_model:{mid}"}])
    txt = f"Models ({prov}, {category}):"
    return txt, kb

def groq_transcribe_audio(audio_bytes, filename="audio.ogg", language="ru", model="whisper-large-v3-turbo"):
    api_key = load_provider_key("groq")
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
    api_key = load_provider_key("groq")
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


def set_bot_commands(token):
    commands = [
        {"command": "provider", "description": "Select provider"},
        {"command": "models", "description": "Select model"},
        {"command": "top", "description": "Top 3 models/providers by delivered answers"},
        {"command": "status", "description": "Show current session status"},
        {"command": "mode", "description": "Engine mode: native/claude/opencode/pi"},
        {"command": "tools", "description": "Tools mode: on/off/status"},
        {"command": "model", "description": "Set model manually"},
        {"command": "stt", "description": "Speech-to-text (send voice/audio next)"},
        {"command": "tts", "description": "Text-to-speech: /tts your text"},
        {"command": "reset", "description": "Reset chat history"},
        {"command": "feedback", "description": "Send feedback to admin"},
        {"command": "version", "description": "Show bot version (commit SHA + build date)"},
    ]

    # Telegram command menu may be scoped and language-specific.
    # Publish commands for default + private chats, both generic and RU locale,
    # so /top is visible in the slash menu for typical private-chat usage.
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

def ask_llm(api_url, api_key, model, messages, uid=None, admin_id=None, use_tools=True, use_proxy=False):
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    meta = {"finish_reason": None, "tool_calls_total": 0, "error": None, "http_latency_ms": 0}
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
            err_body = ""
            try:
                err_body = e.read().decode(errors="replace")
            except Exception:
                err_body = ""
            if e.code == 404: return f"❌ Model `{model}` unavailable.", usage, meta
            if e.code == 429:
                val = e.headers.get("Retry-After") or e.headers.get("x-ratelimit-reset")
                try:
                    v = float(val); v = v/1000 if v > 1e11 else v
                    if v > 1e9: v -= time.time()
                    wait_info = f" (Retry in {format_wait_time(max(0, v))})"
                except: wait_info = f" ({val})"
                return f"❌ Rate limit reached{wait_info}.", usage, meta
            if err_body:
                log.warning(f"HTTP {e.code} from provider for model={model}: {err_body[:400]}")
            return f"❌ HTTP Error: {e}", usage, meta
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
            env["ANTHROPIC_API_KEY"] = ""
            env["ANTHROPIC_BASE_URL"] = base_url.replace("/v1", "")
        except Exception:
            pass
        # Pi-native provider env vars (pi ignores generic OPENAI_API_KEY for
        # non-OpenAI providers and expects provider-specific keys).
        piProviderEnv = {
            "openrouter": "OPENROUTER_API_KEY",
            "groq": "GROQ_API_KEY",
            "cerebras": "CEREBRAS_API_KEY",
            "nvidia": "OPENAI_API_KEY",  # nvidia uses OpenAI-compat
        }
        for pname, evar in piProviderEnv.items():
            pkey = load_provider_key(pname)
            if pkey:
                env[evar] = pkey
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
            settings_path = os.path.join(claude_cfg_dir, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"permissions": {"defaultMode": "bypassPermissions"}}, f)
        except Exception as e:
            log.warning(f"Failed to write claude settings in {claude_cfg_dir}: {e}")
        env["CLAUDE_CONFIG_DIR"] = claude_cfg_dir
        # claude-agent-acp disallows bypassPermissions for root unless IS_SANDBOX is set.
        env["IS_SANDBOX"] = "1"
        env["HTTP_PROXY"] = PROXY_URL
        env["HTTPS_PROXY"] = PROXY_URL
        env["ALL_PROXY"] = PROXY_URL

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
            "-e", "ANTHROPIC_API_KEY=",
            "-e", f"OPENAI_BASE_URL={env.get('OPENAI_BASE_URL', '')}",
            "-e", f"OPENAI_MODEL={mode_model}",
            "-e", f"ANTHROPIC_DEFAULT_OPUS_MODEL={mode_model}",
            "-e", f"ANTHROPIC_DEFAULT_SONNET_MODEL={mode_model}",
            "-e", f"ANTHROPIC_DEFAULT_HAIKU_MODEL={mode_model}",
            "-e", f"CLAUDE_CODE_SUBAGENT_MODEL={mode_model}",
            "-e", "HOME=/workspace/.claude-home",
            "-e", "XDG_CONFIG_HOME=/workspace/.claude-config",
            "-e", "XDG_CACHE_HOME=/workspace/.claude-cache",
            "-e", "CLAUDE_CONFIG_DIR=/workspace/.claude-state",
        ]
        # Pass pi-native provider keys into container.
        for envVar in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY"):
            if env.get(envVar):
                podman_base += ["-e", f"{envVar}={env[envVar]}"]
        if use_proxy:
            podman_base += [
                "-e", f"HTTPS_PROXY={PROXY_URL}",
                "-e", f"HTTP_PROXY={PROXY_URL}",
                "-e", f"ALL_PROXY={PROXY_URL}",
            ]
        podman_base += [
            "-v", f"{cwd}:/workspace",
            "-w", "/workspace",
            "acpx-claude:latest",
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
            # Pi supports only providers with native env var keys.
            # No custom base URL support, so nvidia and others won't work.
            piSupportedProviders = {"openrouter": "openrouter", "groq": "groq", "cerebras": "cerebras"}
            pi_prov = piSupportedProviders.get(provider)
            if not pi_prov:
                supported = ", ".join(sorted(piSupportedProviders))
                return (
                    f"❌ Pi mode does not support provider `{provider}`. "
                    f"Supported: {supported}.\n"
                    f"Switch provider via /provider or use /mode native or /mode claude.",
                    {"prompt_tokens": 0, "completion_tokens": 0},
                    {"finish_reason": "pi_unsupported_provider", "tool_calls_total": 0, "error": f"unsupported provider {provider}", "session_id": session_uuid},
                )
            pi_cmd = ["pi", "-p", "--no-session", "--model", mode_model, "--provider", pi_prov]
            pi_cmd.append(text)
            run_cmd = podman_base + pi_cmd
        else:
            run_cmd = podman_base + [
                "acpx", "--cwd", "/workspace", "--format", "text",
                "--approve-all", "--non-interactive-permissions", "deny",
                "--timeout", acpx_timeout,
                agent, "exec", text,
            ]
        log.info(f"acpx run: {shlex.join(run_cmd[:-1] + ['<task>'])}")
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

def handle_callback(cb, token, admin_id):
    uid = cb["from"]["id"]; data = cb.get("data", "")
    if data.startswith("set_provider:"):
        prov_name = data.split(":", 1)[1]; sess = DB.get_session(uid)
        default_model = PROVIDERS[prov_name]["default_model"]
        default_tools = PROVIDERS[prov_name].get("supports_tools", True)
        DB.save_session(uid, default_model, sess["history"], provider=prov_name, tools_enabled=default_tools, engine_mode=sess.get("engine_mode", "native"))
        tg_request(token, "editMessageText", {"chat_id": cb["message"]["chat"]["id"], "message_id": cb["message"]["message_id"], "text": f"✅ Provider: {prov_name}\nModel: {default_model}\nTools: {'on' if default_tools else 'off'}"})
    elif data.startswith("set_model:"):
        m = sanitize_model_id(data.split(":", 1)[1]); sess = DB.get_session(uid); DB.save_session(uid, m, sess["history"], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode=sess.get("engine_mode", "native"))
        info = DB.get_model_info(sess["provider"], m)
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
        tg_request(token, "sendMessage", {"chat_id": admin_id, "text": f"🔔 New user {uname} (`{uid}`) wants access.", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": admin_kb}})
        tg_request(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Запрос отправлен админу"})

def handle_command(uid, username, text, token, admin_id):
    if text == "/reset":
        sess = DB.get_session(uid)
        DB.save_session(uid, sess["model"], [], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode=sess.get("engine_mode", "native"))
        DB.set_last_session_id(uid, "")
        with runtimeStatusLock:
            runtimeStatus.pop(uid, None)
        u_dir = os.path.join(SESSIONS_ROOT, str(uid))
        if os.path.exists(u_dir): shutil.rmtree(u_dir); os.makedirs(u_dir)
        tg_request(token, "sendMessage", {"chat_id": uid, "text": "Reset done."})
    elif text == "/provider":
        avail = available_providers(); kb = []
        sess = DB.get_session(uid)
        for name in avail:
            icon = "✅ " if name == sess["provider"] else ""
            kb.append([{"text": f"{icon}{name}", "callback_data": f"set_provider:{name}"}])
        tg_request(token, "sendMessage", {"chat_id": uid, "text": "Provider:", "reply_markup": {"inline_keyboard": kb}})
    elif text == "/models":
        sess = DB.get_session(uid)
        txt, kb = build_models_view(sess, category="text", limit=12)
        tg_request(token, "sendMessage", {"chat_id": uid, "text": txt, "reply_markup": {"inline_keyboard": kb}})
    elif text == "/status":
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
        sid_text = sid if sid else "нет (новая сессия — UUID появится после первого ответа)"
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
            f"• Версия: `{__VERSION__}`"
        )
        res = tg_send_text(token, uid, txt, parse_mode="Markdown")
        log.info(f"/status sendMessage result: ok={res.get('ok')} chat_id={uid} desc={(res.get('description') or '')[:200]}")
    elif text == "/top":
        tg_send_text(token, uid, build_top_text(), parse_mode="Markdown")
    elif text.startswith("/model"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            m = sanitize_model_id(parts[1].strip()); sess = DB.get_session(uid); DB.save_session(uid, m, sess["history"], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode=sess.get("engine_mode", "native"))
            tg_send_text(token, uid, f"✅ Model: {m}")
    elif text.startswith("/mode"):
        parts = text.split(maxsplit=1)
        sess = DB.get_session(uid)
        if len(parts) == 1:
            tg_send_text(token, uid, f"Mode: {sess.get('engine_mode', 'native')}\nUse: /mode native or /mode claude or /mode opencode or /mode pi")
        else:
            mode = parts[1].strip().lower()
            if mode not in ("native", "claude", "opencode", "pi"):
                tg_send_text(token, uid, "Use /mode native or /mode claude or /mode opencode or /mode pi")
                return True
            DB.save_session(uid, sess["model"], sess["history"], provider=sess["provider"], tools_enabled=sess["tools_enabled"], engine_mode=mode)
            tg_send_text(token, uid, f"✅ Mode: {mode}")
    elif text.startswith("/tools"):
        parts = text.split(maxsplit=1)
        sess = DB.get_session(uid)
        if len(parts) == 1:
            provider_supports_tools = PROVIDERS.get(sess["provider"], {}).get("supports_tools", True)
            txt = f"Tools: {'on' if sess['tools_enabled'] else 'off'}\nProvider supports tools: {'yes' if provider_supports_tools else 'no'}\nUse: /tools on or /tools off"
            tg_send_text(token, uid, txt)
        else:
            mode = parts[1].strip().lower()
            if mode not in ("on", "off"):
                tg_send_text(token, uid, "Use /tools on or /tools off")
                return True
            enabled = mode == "on"
            if enabled and not PROVIDERS.get(sess["provider"], {}).get("supports_tools", True):
                tg_send_text(token, uid, f"❌ Provider {sess['provider']} does not support tools.")
                return True
            DB.save_session(uid, sess["model"], sess["history"], provider=sess["provider"], tools_enabled=enabled, engine_mode=sess.get("engine_mode", "native"))
            tg_send_text(token, uid, f"✅ Tools: {mode}")
    elif text.startswith("/feedback"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            fb = parts[1].strip(); uname = f"@{username}" if username else f"ID: {uid}"
            tg_request(token, "sendMessage", {"chat_id": admin_id, "text": f"📩 Feedback from {uname}: {fb}"})
            tg_request(token, "sendMessage", {"chat_id": uid, "text": "✅ Sent!"})
    elif text == "/version":
        txt = f"🔖 Version: `{__VERSION__}`"
        tg_request(token, "sendMessage", {"chat_id": uid, "text": txt, "parse_mode": "Markdown"})
    elif text.startswith("/stt"):
        with pendingSttUsersLock:
            pendingSttUsers.add(uid)
        tg_send_text(token, uid, "🎙 Send voice/audio file now. I'll transcribe it.")
    elif text.startswith("/tts"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            tg_send_text(token, uid, "Use: /tts your text")
            return True
        try:
            t0 = time.time()
            source_text = parts[1].strip()
            sess = DB.get_session(uid)
            tts_model = sess.get("model", "")
            audio = groq_tts(source_text, model=tts_model)
            latency_ms = int((time.time() - t0) * 1000)
            res = tg_send_document_bytes(token, uid, "tts.wav", audio, caption="🔊 TTS")
            DB.log_media_request(
                uid,
                "groq",
                tts_model,
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
                "groq",
                DB.get_session(uid).get("model", ""),
                "tts",
                input_size_bytes=len(parts[1].strip().encode("utf-8")) if len(parts) > 1 else 0,
                output_size_bytes=0,
                latency_ms=0,
                ok=False,
                error=str(e),
            )
            tg_send_text(token, uid, f"❌ TTS error: {str(e)[:300]}")
    elif text == "/users" and uid == admin_id:
        stats = DB.get_all_users_stats(); txt = "👥 *Users:*\n"
        for s in stats:
            role = "👑" if s['id'] == admin_id else ("✅" if s['allowed'] else "❌")
            uname = (s['username'] or "Unknown").replace("_", "\\_")
            txt += f"• `{s['id']}` (@{uname}): {role} | Msg: {s['count']} | Tkn: {s['prompt']+s['completion']}\n"
        tg_request(token, "sendMessage", {"chat_id": uid, "text": txt, "parse_mode": "Markdown"})
    return True


def build_top_text():
    top_models = DB.get_top_models(limit=3)
    top_providers = DB.get_top_providers(limit=3)
    if not top_models and not top_providers:
        return "Пока нет данных для топа."
    txt = (
        "📊 *Top Stats*\n"
        "_delivered = ответ действительно отправлен пользователю_\n\n"
        "🏆 *Top 3 Models*\n"
    )
    for i, item in enumerate(top_models, start=1):
        txt += (
            f"{i}. `{item['provider']}/{item['model']}`\n"
            f"   delivered: *{item['delivered']}* | total: *{item['total']}* | success: *{item['success_rate']:.1f}%*\n"
        )
    txt += "\n🏅 *Top 3 Providers*\n"
    for i, item in enumerate(top_providers, start=1):
        txt += (
            f"{i}. `{item['provider']}`\n"
            f"   delivered: *{item['delivered']}* | total: *{item['total']}* | success: *{item['success_rate']:.1f}%*\n"
        )
    return txt

def process_update(upd, token, admin_id):
    try:
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
        sess_for_media = DB.get_session(uid)
        model_for_media = (sess_for_media.get("model") or "").lower()
        auto_stt_model = any(k in model_for_media for k in ("whisper", "speech-to-text", "stt"))
        if (stt_pending or auto_stt_model) and ("voice" in msg or "audio" in msg or "document" in msg):
            try:
                media = msg.get("voice") or msg.get("audio") or msg.get("document")
                file_id = media.get("file_id")
                t0 = time.time()
                file_path, blob = tg_get_file_bytes(token, file_id)
                stt_model = sess_for_media.get("model", "")
                transcript = groq_transcribe_audio(blob, filename=os.path.basename(file_path or "audio.ogg"), model=stt_model)
                latency_ms = int((time.time() - t0) * 1000)
                DB.log_media_request(
                    uid,
                    "groq",
                    stt_model,
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
                    "groq",
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

        allowed = DB.update_and_check(uid, username)
        if uid == admin_id: allowed = True
        if not allowed:
            if is_subscribed(token, uid): DB.set_allowed(uid, True); allowed = True
            else:
                kb = [
                    [{"text": "Sub", "url": f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"}],
                    [{"text": "✅ Done", "callback_data": "check_sub"}],
                    [{"text": "📝 Request access", "callback_data": "request_access"}],
                ]
                tg_request(token, "sendMessage", {"chat_id": uid, "text": f"Sub to {REQUIRED_CHANNEL}\n\nЕсли не хотите подписываться, нажмите «Request access».", "reply_markup": {"inline_keyboard": kb}})
                return
        if text.startswith("/"): return handle_command(uid, username, text, token, admin_id)

        with inflightUsersLock:
            if uid in inflightUsers:
                tg_send_text(token, uid, "⏳ Previous request is still running. Please wait for it to finish.")
                return
            inflightUsers.add(uid)

        sess = DB.get_session(uid); hist = sess["history"]; model = sess["model"]; provider = sess["provider"]
        prov = PROVIDERS.get(provider, PROVIDERS[PROVIDER_DEFAULT])
        api_key = load_provider_key(provider) or load_provider_key(PROVIDER_DEFAULT)
        use_proxy = prov.get("proxy", False)
        use_tools = sess.get("tools_enabled", True) and prov.get("supports_tools", True)

        if estimate_tokens(hist) > MAX_CONTEXT_TOKENS:
            hist = compact_history(prov["url"], api_key, model, hist, uid, admin_id, use_proxy=use_proxy)

        role_desc = "You are an ADMIN with full internet access." if uid == admin_id else "You are a USER. Internet restricted."
        sys_prompt = f"VDS Agent. Instructions: {role_desc} Environment: Alpine Linux. No 'requests' lib, use 'urllib.request', wget, curl. Use DuckDuckGo (html.duckduckgo.com) if Google fails. Output: Telegram Markdown V1 — *bold* (single asterisk, NEVER double), _italic_, `inline code`, triple backticks for code blocks, [text](url) for links. Do NOT use **double asterisks** — Telegram does not render them. Do NOT use # headers — bold the line instead. Be concise — show actual command output, no hypothetical examples, no tables with status, no 'next steps' sections. Just execute and show results. When user sends coordinates [Геолокация: lat, lon], use them for location-based queries (search nearby places, weather, etc.). Always complete your answer fully — never cut off mid-sentence."

        mode = sess.get("engine_mode", "native")
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
        footer = f"[{provider}/{model_short} | In: {usage['prompt_tokens']} | Out: {usage['completion_tokens']} | Ctx: {estimate_tokens(hist)}/{MAX_CONTEXT_TOKENS}{sid_part}]"
        reply = to_telegram_markdown(ans) + "\n\n" + footer
        send_res = tg_send_long_text(token, uid, reply, parse_mode="Markdown")
        DB.set_request_delivered(req_id, bool(send_res.get("ok")))
        with inflightUsersLock:
            inflightUsers.discard(uid)
    except Exception as e:
        log.error(f"process_update error: {e}", exc_info=True)
        try:
            if 'uid' in locals() and uid is not None:
                with inflightUsersLock:
                    inflightUsers.discard(uid)
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
        log.info(f"VDS Agent starting on port 8080. Providers: {avail}. Webhook: {TUNNEL_URL}/{t[:5]}...")
        set_bot_commands(t)
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{t}/setWebhook", f"--B\r\nContent-Disposition: form-data; name=\"url\"\r\n\r\n{TUNNEL_URL}/{t}\r\n--B--\r\n".encode(), {"Content-Type": "multipart/form-data; boundary=B"}))
        ThreadingHTTPServer(("127.0.0.1", 8080), WebhookHandler).serve_forever()
    else: log.error("No token!")
