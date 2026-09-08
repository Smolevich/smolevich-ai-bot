"""Медиа-ветки отвечали по-английски и служебным текстом провайдера.

2026-09-07: «GIF is not supported for video detection by current provider endpoint»,
«Video is too big for Telegram Bot API download (24.0 MB > 20.0 MB)», «❌ STT error:
HTTP 429 …». Человек должен узнать ровно две вещи — что прислать и какой предел.

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

_spec = importlib.util.spec_from_file_location("bot_media_ru", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

ADMIN = 1
USER = 7
SESSION = {"provider": "nvidia", "model": "d", "ui_lang": "ru",
           "tools_enabled": True, "history": [], "engine_mode": "native"}
TOO_BIG = bot.TELEGRAM_BOT_FILE_DOWNLOAD_LIMIT_BYTES + 1


_next_update_id = iter(range(1, 10_000))


def run(msg, stt_pending=False, video_pending=False, transcribe=None, analyse=None):
    """Прогнать одно входящее и собрать всё, что бот сказал в чат.

    update_id каждый раз новый: бот отбрасывает повторы, и второй тест иначе молчит.
    """
    said = []
    with bot.pendingSttUsersLock:
        bot.pendingSttUsers.clear()
        if stt_pending:
            bot.pendingSttUsers.add(USER)
    with bot.pendingVideoUsersLock:
        bot.pendingVideoUsers.clear()
        if video_pending:
            bot.pendingVideoUsers.add(USER)
    with mock.patch.object(bot, "ensure_access", return_value=True), \
         mock.patch.object(bot, "tg_send_text", side_effect=lambda t, u, text, **k: said.append(text) or {"ok": True}), \
         mock.patch.object(bot, "tg_send_long_text", side_effect=lambda t, u, text, **k: said.append(text) or {"ok": True}), \
         mock.patch.object(bot, "capabilities_for_model", return_value=[]), \
         mock.patch.object(bot, "pick_video_detector", return_value=("nvidia", "d")), \
         mock.patch.object(bot, "load_provider_key", return_value="k"), \
         mock.patch.object(bot, "tg_get_file_bytes", return_value=("f.mp4", b"x")), \
         mock.patch.object(bot, "allow_voice_use", return_value=True), \
         mock.patch.object(bot, "analyze_video_detection",
                           side_effect=analyse or (lambda *a, **k: "*Real* (confidence 90.0%)")), \
         mock.patch.object(bot, "transcribe_audio_with_fallback",
                           side_effect=transcribe or (lambda *a, **k: ("привет", "groq", "w"))), \
         mock.patch.object(bot.DB, "get_session", return_value=dict(SESSION)), \
         mock.patch.object(bot.DB, "get_model_info", return_value={"available": True}), \
         mock.patch.object(bot.DB, "pick_default_stt_model", return_value=("groq", "w")), \
         mock.patch.object(bot.DB, "log_media_request"):
        bot.process_update({"update_id": next(_next_update_id),
                            "message": dict(msg, **{"from": {"id": USER, "username": "u"}})},
                           "token", ADMIN)
    return " ".join(said)


def has_cyrillic(text):
    return any("а" <= ch.lower() <= "я" for ch in text)


class WhatToSend(unittest.TestCase):
    def test_a_gif_is_refused_in_russian(self):
        said = run({"document": {"file_id": "g", "mime_type": "image/gif", "file_size": 10}}, video_pending=True)
        self.assertTrue(has_cyrillic(said), said)

    def test_a_gif_refusal_names_the_formats_to_send_instead(self):
        said = run({"document": {"file_id": "g", "mime_type": "image/gif", "file_size": 10}}, video_pending=True)
        self.assertIn("MP4", said)


class WhatTheLimitIs(unittest.TestCase):
    def test_an_oversized_video_is_refused_in_russian(self):
        said = run({"video": {"file_id": "v", "mime_type": "video/mp4", "file_size": TOO_BIG}}, video_pending=True)
        self.assertTrue(has_cyrillic(said), said)

    def test_an_oversized_video_names_the_ceiling(self):
        said = run({"video": {"file_id": "v", "mime_type": "video/mp4", "file_size": TOO_BIG}}, video_pending=True)
        self.assertIn(bot.format_bytes(bot.TELEGRAM_BOT_FILE_DOWNLOAD_LIMIT_BYTES), said)

    def test_an_oversized_video_never_mentions_the_bot_api(self):
        said = run({"video": {"file_id": "v", "mime_type": "video/mp4", "file_size": TOO_BIG}}, video_pending=True)
        self.assertNotIn("Bot API", said)

    def test_an_oversized_audio_names_the_ceiling(self):
        said = run({"voice": {"file_id": "a", "file_size": TOO_BIG}}, stt_pending=True)
        self.assertIn(bot.format_bytes(bot.TELEGRAM_BOT_FILE_DOWNLOAD_LIMIT_BYTES), said)


class NoProviderErrorsInTheChat(unittest.TestCase):
    def boom(self, *a, **k):
        raise RuntimeError("HTTP 429: Too Many Requests {\"error\": ...}")

    def test_a_failed_transcription_hides_the_http_body(self):
        said = run({"voice": {"file_id": "a", "file_size": 10}}, stt_pending=True, transcribe=self.boom)
        self.assertNotIn("HTTP 429", said)

    def test_a_failed_transcription_still_says_something_in_russian(self):
        said = run({"voice": {"file_id": "a", "file_size": 10}}, stt_pending=True, transcribe=self.boom)
        self.assertTrue(has_cyrillic(said), said)

    def test_a_failed_video_check_hides_the_http_body(self):
        said = run({"video": {"file_id": "v", "mime_type": "video/mp4", "file_size": 10}},
                   video_pending=True, analyse=self.boom)
        self.assertNotIn("HTTP 429", said)


class HeadingsAreRussian(unittest.TestCase):
    def test_the_transcript_is_not_labelled_transcription(self):
        said = run({"voice": {"file_id": "a", "file_size": 10}}, stt_pending=True)
        self.assertNotIn("Transcription", said)


if __name__ == "__main__":
    unittest.main()
