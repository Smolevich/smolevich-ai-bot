from __future__ import annotations

import re
from typing import Any

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
MDV2_UNESCAPE_RE = re.compile(r"\\([_*\[\]()~`>#+\-=|{}.!\\])")


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    text = "".join([m.get("content", "") or "" for m in messages])
    return len(text) // 4


def compact_messages_for_provider(messages: list[dict[str, Any]], keep_recent: int = 8) -> list[dict[str, Any]]:
    if not messages:
        return []
    system: list[dict[str, Any]] = []
    non_system: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "system":
            system.append(message)
        else:
            non_system.append(message)
    return system + non_system[-keep_recent:]


def sanitize_model_id(model: str | None) -> str:
    value = str(model or "")
    value = ANSI_RE.sub("", value)
    value = "".join(ch for ch in value if ch.isprintable())
    return value.strip()


def to_telegram_markdown(text: str) -> str:
    if not text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("```", i):
            end = text.find("```", i + 3)
            if end == -1:
                out.append(text[i:])
                break
            out.append(text[i:end + 3])
            i = end + 3
            continue
        if text[i] == "`":
            end = text.find("`", i + 1)
            if end == -1:
                out.append(text[i])
                i += 1
                continue
            out.append(text[i:end + 1])
            i = end + 1
            continue
        if text[i] == "#" and (i == 0 or text[i - 1] == "\n"):
            j = i
            while j < n and text[j] == "#":
                j += 1
            if j < n and text[j] == " ":
                line_end = text.find("\n", j)
                if line_end == -1:
                    line_end = n
                line = text[j + 1:line_end].strip()
                out.append("*" + line + "*")
                i = line_end
                continue
        if text.startswith("**", i):
            out.append("*")
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def split_telegram_text(text: str, max_len: int = 3500) -> list[str]:
    value = text or ""
    if len(value) <= max_len:
        return [value]
    parts: list[str] = []
    buf = value
    while len(buf) > max_len:
        cut = buf.rfind("\n", 0, max_len)
        if cut < int(max_len * 0.5):
            cut = buf.rfind(" ", 0, max_len)
        if cut < int(max_len * 0.5):
            cut = max_len
        parts.append(buf[:cut].rstrip())
        buf = buf[cut:].lstrip()
    if buf:
        parts.append(buf)
    return parts


def to_telegram_markdown_v2(text: str) -> str:
    """Convert markdown-ish text to Telegram-safe MarkdownV2.

    Keeps a small subset of formatting (`*bold*`, `_italic_`, inline links, inline/fenced code)
    and escapes everything else required by Telegram MarkdownV2.
    """
    if not text:
        return text

    value = to_telegram_markdown(text)
    tokens: list[str] = []

    def put_token(raw: str) -> str:
        tokens.append(raw)
        return f"\x00{len(tokens)-1}\x00"

    # Protect fenced code blocks first.
    value = re.sub(r"```[\s\S]*?```", lambda m: put_token(m.group(0)), value)
    # Protect inline code.
    value = re.sub(r"`[^`\n]+`", lambda m: put_token(m.group(0)), value)

    # Protect inline links.
    def protect_link(match: re.Match[str]) -> str:
        label = MDV2_ESCAPE_RE.sub(r"\\\1", match.group(1))
        url = match.group(2).replace("\\", "\\\\").replace(")", "\\)")
        return put_token(f"[{label}]({url})")

    value = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", protect_link, value)

    # Protect basic emphasis. Require at least one word char to avoid list bullets.
    value = re.sub(r"(?<!\*)\*([^\n*]*\w[^\n*]*)\*(?!\*)", lambda m: put_token(f"*{m.group(1)}*"), value)
    value = re.sub(r"(?<!_)_([^\n_]*\w[^\n_]*)_(?!_)", lambda m: put_token(f"_{m.group(1)}_"), value)

    # Escape everything else for MarkdownV2.
    value = MDV2_ESCAPE_RE.sub(r"\\\1", value)

    # Restore protected tokens.
    for i, raw in enumerate(tokens):
        value = value.replace(f"\x00{i}\x00", raw)
    return value


def strip_markdown_v2_escapes(text: str) -> str:
    if not text:
        return text
    return MDV2_UNESCAPE_RE.sub(r"\1", text)
