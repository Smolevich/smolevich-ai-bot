"""The button must show the published board — and order it by something a human can check.

Until 2026-08-12 the button showed delivery stats of the bot's own requests instead,
and the published `rank` put a model solving 7/10 above one solving 9/10.

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

_spec = importlib.util.spec_from_file_location("bot_board", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

PAYLOAD = {
    "updatedAt": "2026-08-12T07:01:41.216Z",
    "models": [
        {"rank": 1, "model": "cohere/north-mini-code:free", "provider": "OpenRouter", "scores": {"native": 0.722}},
        {"rank": 2, "model": "meta/llama-3.2-11b", "provider": "NVIDIA", "scores": {"native": 0.66}},
        {"rank": 3, "model": "llama-3.1-8b-instant", "provider": "Groq", "scores": {"native": 0.891}},
    ],
}


def view(payload, is_en=False):
    with mock.patch.object(bot, "fetch_leaderboard", return_value=payload):
        return bot.build_leaderboard_view(is_en=is_en)


def callbacks(kb):
    return [b["callback_data"] for row in kb for b in row]


class Ordering(unittest.TestCase):
    def test_best_solver_comes_first_despite_published_rank(self):
        txt, _ = view(PAYLOAD)
        self.assertLess(txt.index("llama-3.1-8b-instant"), txt.index("north-mini-code"))

    def test_unmeasured_model_sinks_below_measured_ones(self):
        payload = {"models": [{"model": "a/unknown", "provider": "Groq", "scores": {}},
                              {"model": "b/known", "provider": "Groq", "scores": {"native": 0.5}}]}
        txt, _ = view(payload)
        self.assertLess(txt.index("known"), txt.index("unknown"))


class Wording(unittest.TestCase):
    def test_pass_rate_is_shown_as_whole_answers(self):
        txt, _ = view(PAYLOAD)
        self.assertIn("решает 9 из 10", txt)

    def test_no_developer_scores_leak_into_the_screen(self):
        txt, _ = view(PAYLOAD)
        for word in ("score", "overall", "native", "0.891", "uptime"):
            self.assertNotIn(word, txt.lower())

    def test_unmeasured_model_says_so_instead_of_showing_zero(self):
        payload = {"models": [{"model": "a/unknown", "provider": "Groq", "scores": {}}]}
        txt, _ = view(payload)
        self.assertIn("данных пока мало", txt)


class TryButtons(unittest.TestCase):
    def test_every_row_offers_to_try_that_model(self):
        _, kb = view(PAYLOAD)
        self.assertEqual(len([c for c in callbacks(kb) if c.startswith("try:")]), 3)

    def test_callback_stays_within_telegram_64_byte_limit(self):
        _, kb = view(PAYLOAD)
        for data in callbacks(kb):
            self.assertLessEqual(len(data.encode()), 64, data)

    def test_callback_carries_the_provider_not_just_the_model(self):
        _, kb = view(PAYLOAD)
        groq = [c for c in callbacks(kb) if c.endswith("llama-3.1-8b-instant")][0]
        self.assertEqual(bot.PROVIDER_BY_CODE[groq.split(":")[1]], "groq")


class EmptyBoard(unittest.TestCase):
    def test_missing_payload_does_not_crash_the_button(self):
        txt, kb = view(None)
        self.assertIn("Замеры", txt)
        self.assertTrue(kb)


if __name__ == "__main__":
    unittest.main()
