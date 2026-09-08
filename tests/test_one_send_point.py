"""Regression: the scratchpad is cut where every branch passes, not in one of them.

Three defences against a leaked chain of thought were built into the native branch —
`reasoning: exclude` on the request, ignoring the provider's reasoning fields, cutting
`[thinking]` out of the text. The sandbox branch had none of them, so on 2026-09-08 the
admin's greeting came back as:

    [thinking] The user greeted me in Russian with "так привеи"… This is just a casual
    greeting, not a software engineering task.
    Привет! Чем могу помочь?

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

_spec = importlib.util.spec_from_file_location("bot_send_point", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)


def sent(text):
    """What send_model_answer actually puts on the wire."""
    said = []
    with mock.patch.object(bot, "tg_send_long_text",
                           side_effect=lambda t, u, txt, **k: said.append(txt) or {"ok": True}):
        bot.send_model_answer("token", 1, text)
    return said[0]


class ThinkingNeverReachesTheChat(unittest.TestCase):
    def test_an_unclosed_thinking_marker_is_cut(self):
        leaked = ("[thinking] The user greeted me in Russian, this is a casual greeting.\n"
                  "Привет! Чем могу помочь?")
        self.assertEqual(sent(leaked), "Привет! Чем могу помочь?")

    def test_a_closed_think_block_is_cut(self):
        self.assertEqual(sent("<think>перебираю варианты</think>Ответ."), "Ответ.")

    def test_an_ordinary_answer_is_left_alone(self):
        self.assertEqual(sent("Привет! Чем могу помочь?"), "Привет! Чем могу помочь?")

    def test_a_blank_line_between_the_marker_and_the_answer_is_not_kept(self):
        self.assertEqual(sent("[thinking] думаю\n\n\nПривет."), "Привет.")


if __name__ == "__main__":
    unittest.main()
