"""«Сейчас недоступно» без «когда» — это тупик, а не ответ.

2026-09-07: кнопка расшифровки отвечала «Расшифровка сейчас недоступна.» и на этом
разговор кончался. Если провайдер сказал, через сколько возвращаться, — надо назвать
это число; если не сказал — не выдумывать и не печатать «(None)», как было в лимитах.

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

from agent.text import unavailable_message  # noqa: E402

_spec = importlib.util.spec_from_file_location("bot_unavailable", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

SESSION = {"provider": "groq", "model": "llama-3.3-70b-versatile", "ui_lang": "ru",
           "tools_enabled": True, "history": [], "engine_mode": "native"}


class Wording(unittest.TestCase):
    def test_a_known_retry_time_is_named_in_minutes(self):
        self.assertIn("5 мин", unavailable_message("stt", retry_after_sec=300))

    def test_an_unknown_retry_time_leaves_out_the_number(self):
        text = unavailable_message("stt")
        self.assertFalse(any(ch.isdigit() for ch in text), text)

    def test_an_unknown_retry_time_never_prints_none(self):
        """Регресс: в тексте лимита однажды вылезло литеральное «(None)»."""
        self.assertNotIn("None", unavailable_message("tts"))

    def test_the_word_transcription_is_used_not_stt(self):
        self.assertNotIn("stt", unavailable_message("stt").lower())

    def test_the_word_voicing_is_used_not_tts(self):
        self.assertNotIn("tts", unavailable_message("tts").lower())

    def test_video_check_is_not_called_videodetect(self):
        self.assertNotIn("videodetect", unavailable_message("video").lower())


class RememberingWhatTheProviderSaid(unittest.TestCase):
    def setUp(self):
        with bot.featureRetryAfterLock:
            bot.featureRetryAfter.clear()

    def test_a_recorded_retry_time_is_offered_back(self):
        bot.note_feature_retry_after("stt", 120, now=1000)
        self.assertAlmostEqual(bot.feature_retry_after_sec("stt", now=1000), 120, delta=1)

    def test_a_retry_time_that_has_passed_is_forgotten(self):
        bot.note_feature_retry_after("stt", 120, now=1000)
        self.assertIsNone(bot.feature_retry_after_sec("stt", now=2000))

    def test_nothing_recorded_means_no_number(self):
        self.assertIsNone(bot.feature_retry_after_sec("tts", now=1000))


class TheButtonSaysWhenToComeBack(unittest.TestCase):
    def setUp(self):
        with bot.featureRetryAfterLock:
            bot.featureRetryAfter.clear()

    def said(self, action):
        out = []
        with mock.patch.object(bot, "tg_send_text", side_effect=lambda t, u, text, **k: out.append(text)), \
             mock.patch.object(bot, "tg_request", return_value={"ok": True}), \
             mock.patch.object(bot.DB, "get_session", return_value=dict(SESSION)), \
             mock.patch.object(bot.DB, "log_ui_event"), \
             mock.patch.object(bot, "has_stt_models", return_value=False), \
             mock.patch.object(bot, "has_tts_models", return_value=False):
            bot.handle_quick_action(action, 7, "token", admin_id=1)
        return " ".join(out)

    def test_transcription_names_the_minutes_the_provider_gave(self):
        bot.note_feature_retry_after("stt", 300)
        self.assertIn("мин", self.said("stt"))

    def test_transcription_without_a_number_still_says_try_later(self):
        self.assertIn("позже", self.said("stt"))


if __name__ == "__main__":
    unittest.main()
