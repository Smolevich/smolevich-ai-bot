"""Regression: the model's scratchpad must never reach the chat.

2026-09-07, minimax-m3 on OpenRouter answered a question about LinkedIn with
"[thinking] The user is asking in Russian about LinkedIn and Threads…" followed by the
real answer. Both shapes are covered: the separate `reasoning` field OpenRouter returns
and an inline `<think>` / `[thinking]` block.

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from agent.text import answer_from_message, strip_reasoning  # noqa: E402

_spec = importlib.util.spec_from_file_location("bot_reasoning", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

LEAKED = (
    "[thinking] The user is asking in Russian about LinkedIn and Threads.\n"
    "**LinkedIn** — соцсеть для профессионалов."
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def ask_with_message(message):
    payload = {"choices": [{"message": message, "finish_reason": "stop"}], "usage": {}}
    opener = mock.Mock()
    opener.open.return_value = FakeResponse(payload)
    with mock.patch.object(bot, "make_opener", return_value=opener):
        answer, _usage, _meta = bot.ask_llm("https://x/v1/chat/completions", "k", "m",
                                            [{"role": "user", "content": "?"}], use_tools=False)
    return answer


class ThinkingNeverReachesTheUser(unittest.TestCase):
    def test_bracket_thinking_prefix_is_cut_from_the_answer(self):
        self.assertEqual(strip_reasoning(LEAKED), "**LinkedIn** — соцсеть для профессионалов.")

    def test_think_tags_are_cut_from_the_answer(self):
        text = "<think>Let me consider the options.</think>\n\nBelgrade is the capital."
        self.assertEqual(strip_reasoning(text), "Belgrade is the capital.")

    def test_separate_reasoning_field_is_never_used_as_the_answer(self):
        message = {"content": "Belgrade.", "reasoning": "The user asks about Serbia's capital."}
        self.assertEqual(answer_from_message(message), "Belgrade.")

    def test_reasoning_content_field_is_never_used_as_the_answer(self):
        message = {"content": "Belgrade.", "reasoning_content": "Serbia's capital is Belgrade."}
        self.assertNotIn("Serbia", answer_from_message(message))

    def test_ask_llm_strips_an_inline_thinking_block(self):
        self.assertEqual(ask_with_message({"content": LEAKED}),
                         "**LinkedIn** — соцсеть для профессионалов.")

    def test_ask_llm_does_not_append_the_reasoning_field(self):
        answer = ask_with_message({"content": "Belgrade.", "reasoning": "Thinking about Serbia."})
        self.assertNotIn("Thinking", answer or "")


class OrdinaryTextSurvives(unittest.TestCase):
    def test_a_sentence_about_thinking_is_not_stripped(self):
        text = "Я думаю, что <b>это</b> нормально."
        self.assertEqual(strip_reasoning(text), text)

    def test_empty_input_stays_empty(self):
        self.assertEqual(strip_reasoning(None), "")


if __name__ == "__main__":
    unittest.main()
