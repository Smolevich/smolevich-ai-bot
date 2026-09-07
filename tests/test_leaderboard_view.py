"""The measurement, split in two: names for people, numbers for the admin.

Until 2026-09-07 the second screen a newcomer saw was ten rows of
`minimax-m3:free · OpenRouter · решает 8 из 10` with "нажми номер — отвечу этой
моделью". That screen asked the person to do the bot's job.

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

SESSION = {"provider": "groq", "model": "llama-3.1-8b-instant", "ui_lang": "ru"}

PAYLOAD = {
    "updatedAt": "2026-09-07T07:20:18.441Z",
    "models": [
        {"rank": 1, "model": "cohere/north-mini-code:free", "provider": "OpenRouter", "scores": {"native": 0.722}},
        {"rank": 2, "model": "meta/llama-3.2-11b", "provider": "NVIDIA", "scores": {"native": 0.66}},
        {"rank": 3, "model": "llama-3.1-8b-instant", "provider": "Groq", "scores": {"native": 0.891}},
    ],
}

RANKED = [("groq", "llama-3.1-8b-instant"),
          ("openrouter", "cohere/north-mini-code:free"),
          ("nvidia", "meta/llama-3.2-11b")]


def curious(ranked=None):
    with mock.patch.object(bot, "live_model_ranking", return_value=list(RANKED if ranked is None else ranked)):
        return bot.build_curious_view(SESSION)


def admin_text(payload=PAYLOAD):
    with mock.patch.object(bot, "fetch_leaderboard", return_value=payload):
        return bot.build_board_admin_text()


def callbacks(kb):
    return [b["callback_data"] for row in kb for b in row]


class TheCuriousScreen(unittest.TestCase):
    def test_no_more_than_five_rows(self):
        many = [("groq", f"m-{i}") for i in range(20)]
        txt, _ = curious(many)
        self.assertLessEqual(len([line for line in txt.splitlines() if line.startswith("• ")]),
                             bot.CURIOUS_ROWS)

    def test_the_steadiest_is_marked_and_only_once(self):
        txt, _ = curious()
        self.assertEqual(txt.count("✔ самая стабильная"), 1)
        self.assertIn("Llama 3.1 8B Instant  ✔ самая стабильная", txt)

    def test_no_benchmark_numbers_on_a_users_screen(self):
        txt, _ = curious()
        for jargon in ("из 10", "of 10", "Замерено", "Measured", "score"):
            self.assertNotIn(jargon, txt)

    def test_no_model_ids_and_no_provider_names_on_a_users_screen(self):
        txt, _ = curious()
        for jargon in (":free", "OpenRouter", "openrouter", "nvidia", "cohere/"):
            self.assertNotIn(jargon, txt)

    def test_it_says_choosing_is_optional(self):
        txt, _ = curious()
        self.assertIn("Выбирать не обязательно", txt)

    def test_every_name_offers_to_try_that_model(self):
        _, kb = curious()
        self.assertEqual(len([c for c in callbacks(kb) if c.startswith("try:")]), 3)

    def test_callback_stays_within_telegram_64_byte_limit(self):
        _, kb = curious()
        for data in callbacks(kb):
            self.assertLessEqual(len(data.encode()), 64, data)

    def test_callback_carries_the_provider_not_just_the_model(self):
        _, kb = curious()
        groq = [c for c in callbacks(kb) if c.endswith("llama-3.1-8b-instant")][0]
        self.assertEqual(bot.PROVIDER_BY_CODE[groq.split(":")[1]], "groq")

    def test_an_empty_ranking_does_not_dead_end(self):
        txt, kb = curious([])
        self.assertTrue(txt)
        self.assertTrue(kb[-1][0]["text"].startswith("←"))


class TheAdminBoard(unittest.TestCase):
    def test_best_solver_comes_first_despite_published_rank(self):
        txt = admin_text()
        self.assertLess(txt.index("llama-3.1-8b-instant"), txt.index("north-mini-code"))

    def test_the_numbers_survive_where_the_admin_can_see_them(self):
        txt = admin_text()
        self.assertIn("решает 9 из 10", txt)
        self.assertIn("Замерено 2026-09-07", txt)

    def test_an_empty_board_does_not_crash(self):
        self.assertIn("Борд пуст", admin_text(None))


if __name__ == "__main__":
    unittest.main()
