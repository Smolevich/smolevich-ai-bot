import json
import re

from yoyo import step

__depends__ = {"0014_sessions_back_to_native"}

# What the bot said is what it will say again: a stored assistant turn holding
# `<tool_call>curl …</arg_value></tool_call>` is in the context of every following
# question, and models imitate it. The answers were saved before the strip existed, so
# the rows carry exactly the text we now refuse to send. Turns are rewritten in place —
# the conversation keeps its shape, only the make-believe call goes.
_PATTERNS = [
    re.compile(r"<tool_call>.*?(?:</tool_call>|$)", re.DOTALL | re.IGNORECASE),
    re.compile(r"<function_call>.*?(?:</function_call>|$)", re.DOTALL | re.IGNORECASE),
    re.compile(r"<tool_use>.*?(?:</tool_use>|$)", re.DOTALL | re.IGNORECASE),
    re.compile(r"<\|tool_call_start\|>.*?(?:<\|tool_call_end\|>|$)", re.DOTALL | re.IGNORECASE),
    re.compile(r"</?(?:arg_key|arg_value|parameter|invoke)\s*[^>]*>", re.IGNORECASE),
]


def scrub(conn):
    cursor = conn.cursor()
    rows = cursor.execute("SELECT user_id, history_json FROM sessions").fetchall()
    for user_id, raw in rows:
        try:
            history = json.loads(raw or "[]")
        except (ValueError, TypeError):
            continue
        changed = False
        cleaned = []
        for message in history:
            content = message.get("content")
            if message.get("role") == "assistant" and isinstance(content, str):
                new = content
                for pattern in _PATTERNS:
                    new = pattern.sub("", new)
                new = re.sub(r"\n{3,}", "\n\n", new).strip()
                if new != content:
                    changed = True
                    if not new:
                        # An answer that was only a pretend call leaves no answer behind, so
                        # the question it followed goes with it: user/assistant must stay
                        # alternating or the next request is rejected by the provider.
                        if cleaned and cleaned[-1].get("role") == "user":
                            cleaned.pop()
                        continue
                    message = dict(message, content=new)
            cleaned.append(message)
        if changed:
            cursor.execute("UPDATE sessions SET history_json = ? WHERE user_id = ?",
                           (json.dumps(cleaned), user_id))


steps = [step(scrub)]
