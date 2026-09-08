"""Tool calls a model typed out as text instead of asking for one.

A model with no tool schema in the request still knows the shape of a tool call from its
own training, and when the system prompt promises it a shell it writes one out in prose.
2026-09-08 the admin asked for news and got back, as the whole answer:

    <tool_call>curl -s "https://html.duckduckgo.com/html/?q=главные+новости" | head -10
    </arg_value>
    </tool_call>

Every family spells it differently, so the shapes below are the ones we have actually
seen from models we route to, probed on the box the same day:

    Hermes / Qwen      <tool_call>…</tool_call>, with stray </arg_value>, <arg_key>
    Liquid LFM         <|tool_call_start|>[bash(command='…')]<|tool_call_end|>
    Llama              <|python_tag|>…
    Mistral            [TOOL_CALLS] […]
    DeepSeek           <｜tool▁calls▁begin｜>…
    Gemma              ```tool_code … ```
    Anthropic-ish      <tool_use>…</tool_use>, <function_call>…</function_call>

Nothing here talks to the network, the database or a container: text in, text out.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Openers that carry a payload up to a matching closer, and lone markers that swallow the
# rest of the line. Both lists stay narrow on purpose: a false positive eats a real answer.
_PAIRS: tuple[tuple[str, str], ...] = (
    (r"<tool_call>", r"</tool_call>"),
    (r"<tool_calls>", r"</tool_calls>"),
    (r"<function_call>", r"</function_call>"),
    (r"<function_calls>", r"</function_calls>"),
    (r"<tool_use>", r"</tool_use>"),
    (r"<\|tool_call_start\|>", r"<\|tool_call_end\|>"),
    (r"<\|tool_calls_begin\|>", r"<\|tool_calls_end\|>"),
    (r"<[｜|]tool▁calls▁begin[｜|]>", r"<[｜|]tool▁calls▁end[｜|]>"),
    (r"```tool_code", r"```"),
    (r"```tool_call", r"```"),
)

_LONE: tuple[str, ...] = (
    r"<\|tool_call\|>",
    r"<\|python_tag\|>",
    r"\[TOOL_CALLS\]",
    r"<[｜|]tool▁call[｜|]>",
)

# Tags that arrive orphaned when the model loses track of its own format — the reported
# answer ended with a `</arg_value>` whose opener never appeared.
_ORPHAN_TAG_RE = re.compile(
    r"</?(?:tool_call|tool_calls|function_call|function_calls|tool_use|arg_key|arg_value|"
    r"parameter|invoke|tool_response)\s*[^>]*>",
    re.IGNORECASE)

_BLOCK_RES = [
    (re.compile(opener, re.IGNORECASE), re.compile(closer, re.IGNORECASE))
    for opener, closer in _PAIRS
]
_LONE_RES = [re.compile(marker, re.IGNORECASE) for marker in _LONE]

_COMMAND_KWARG_RE = re.compile(r"command\s*=\s*(['\"])(?P<cmd>.*?)\1", re.DOTALL)
_SHELL_HEAD_RE = re.compile(r"^\s*(?:sudo\s+)?(curl|wget|python3?|sh|bash|ls|cat|grep|echo|"
                            r"date|uname|df|free|ps|head|tail|awk|sed|jq)\b")


def find_pseudo_calls(text: str | None) -> list[tuple[int, int, str]]:
    """(start, end, inner) for every textual tool call in `text`, left to right.

    An opener with no closer runs to the end of the text: a truncated call is still a
    call, and leaving its tail in the chat is the bug being fixed.
    """
    value = text or ""
    found: list[tuple[int, int, str]] = []
    for opener_re, closer_re in _BLOCK_RES:
        for opener in opener_re.finditer(value):
            closer = closer_re.search(value, opener.end())
            end = closer.end() if closer else len(value)
            inner = value[opener.end():closer.start() if closer else len(value)]
            found.append((opener.start(), end, inner))
    for marker_re in _LONE_RES:
        for marker in marker_re.finditer(value):
            newline = value.find("\n", marker.end())
            end = len(value) if newline == -1 else newline
            found.append((marker.start(), end, value[marker.end():end]))
    return _drop_nested(sorted(found))


def _drop_nested(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    kept: list[tuple[int, int, str]] = []
    for span in spans:
        if kept and span[0] < kept[-1][1]:
            continue
        kept.append(span)
    return kept


def strip_pseudo_calls(text: str | None) -> str:
    """`text` with every textual tool call and every orphaned tag removed."""
    value = text or ""
    for start, end, _ in reversed(find_pseudo_calls(value)):
        value = value[:start] + value[end:]
    value = _ORPHAN_TAG_RE.sub("", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def command_from_pseudo_call(inner: str | None) -> str | None:
    """The shell command a textual call asks for, or None when it is not plainly one.

    Uncertainty means None: the caller strips the block instead of running something it
    had to guess at.
    """
    raw = _ORPHAN_TAG_RE.sub("", inner or "").strip()
    if not raw:
        return None
    payload = _json_payload(raw)
    if payload is not None:
        name = str(payload.get("name") or payload.get("tool") or "")
        args = payload.get("arguments") or payload.get("parameters") or {}
        command = args.get("command") if isinstance(args, dict) else None
        if name in ("execute_bash", "bash", "shell", "run_bash") and isinstance(command, str):
            return command.strip() or None
        return None
    kwarg = _COMMAND_KWARG_RE.search(raw)
    if kwarg:
        return kwarg.group("cmd").strip() or None
    if _SHELL_HEAD_RE.match(raw):
        return raw.strip() or None
    return None


def _json_payload(raw: str) -> dict[str, Any] | None:
    start = raw.find("{")
    if start == -1:
        return None
    try:
        parsed = json.loads(raw[start:raw.rfind("}") + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None
