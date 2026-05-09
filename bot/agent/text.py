from __future__ import annotations

import re
from typing import Any

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


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
    value = _ANSI_RE.sub("", value)
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

