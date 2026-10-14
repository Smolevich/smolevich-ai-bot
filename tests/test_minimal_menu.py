"""Regression (PP-292): a newcomer sees one button, and the first screen holds only features.

Stas, 15.10.2026: «давай его сделаем максимально ещё проще с точки зрения меню». The
bottom row carried «💬 Спросить» and «🎙 Аудио → текст» — both only told a person what
a plain message or a voice note already does. «Для любопытных» sat on the first screen
under ☰ although nobody new needs it. Retired providers must never be named to a user.
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

_spec = importlib.util.spec_from_file_location("bot_minimal_menu", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

SESSION = {"provider": "groq", "model": "llama-3.3-70b-versatile", "ui_lang": "ru",
           "tools_enabled": True, "history": [], "engine_mode": "native"}
RETIRED = ("cerebras", "hugging")


def bottom_labels():
    with mock.patch.object(bot.DB, "get_session", return_value=dict(SESSION)):
        kb = bot.quick_keyboard(7)
    return [b["text"] for row in kb["keyboard"] for b in row]


def root_callbacks(stt=True):
    with mock.patch.object(bot, "has_stt_models", return_value=stt), \
         mock.patch.object(bot, "has_tts_models", return_value=False), \
         mock.patch.object(bot, "has_video_detector", return_value=False):
        _, kb = bot.build_menu_root(SESSION, is_admin=False)
    return [b["callback_data"] for row in kb for b in row]


def user_screen_texts():
    with mock.patch.object(bot, "has_stt_models", return_value=True), \
         mock.patch.object(bot, "has_tts_models", return_value=True), \
         mock.patch.object(bot, "has_video_detector", return_value=True), \
         mock.patch.object(bot, "live_model_ranking", return_value=[("groq", "a/b")]), \
         mock.patch.object(bot, "live_latency_map", return_value={}):
        screens = [bot.build_menu_root(SESSION), bot.build_menu_settings(SESSION),
                   bot.build_curious_view(SESSION)]
    texts = [bot.build_help_text(SESSION)]
    for title, kb in screens:
        texts.append(title)
        texts += [b["text"] for row in kb for b in row]
    return " ".join(texts).lower()


class MinimalMenu(unittest.TestCase):
    def test_the_bottom_row_is_a_single_more_button(self):
        self.assertEqual(bottom_labels(), [bot.QUICK_MORE["ru"]])

    def test_the_first_screen_under_more_is_features_and_settings_only(self):
        self.assertEqual(root_callbacks(), ["menu:stt", "menu:settings"])

    def test_transcription_is_still_one_tap_away_under_more(self):
        self.assertIn("menu:stt", root_callbacks(stt=True))

    def test_the_curious_screen_returns_to_settings(self):
        with mock.patch.object(bot, "live_model_ranking", return_value=[]):
            _, kb = bot.build_curious_view(SESSION)
        self.assertEqual(kb[-1][0]["callback_data"], "menu:settings")

    def test_no_user_screen_names_a_retired_provider(self):
        texts = user_screen_texts()
        self.assertFalse([p for p in RETIRED if p in texts])


if __name__ == "__main__":
    unittest.main()
