"""Gate, photo, feedback and the video mode — the leftovers from the 2026-08-02 review.

Regressions locked in here:
- voice and video reached the model without passing the access gate (it ran below them);
- photos were dropped in silence, which reads as a broken bot;
- the feedback callback taught a /feedback command handle_command did not know;
- entering video detection overwrote the user's chosen LLM in the session.

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

_spec = importlib.util.spec_from_file_location("bot_gate", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

ADMIN = 1
STRANGER = 777


def voice_update(uid):
    return {"update_id": 1, "message": {"from": {"id": uid, "username": "u"}, "voice": {"file_id": "x", "file_size": 10}}}


def photo_update(uid):
    return {"update_id": 2, "message": {"from": {"id": uid, "username": "u"}, "photo": [{"file_id": "p"}]}}


class AccessGate(unittest.TestCase):
    def test_voice_from_a_stranger_never_reaches_transcription(self):
        with mock.patch.object(bot, "ensure_access", return_value=False) as gate, \
             mock.patch.object(bot, "DB") as db:
            bot.process_update(voice_update(STRANGER), "token", ADMIN)
        gate.assert_called_once()
        db.get_session.assert_not_called()

    def test_gate_lets_an_allowed_person_through(self):
        with mock.patch.object(bot.DB, "update_and_check", return_value=True), \
             mock.patch.object(bot, "tg_request") as send:
            self.assertTrue(bot.ensure_access(STRANGER, "u", "token", ADMIN))
        send.assert_not_called()

    def test_gate_shows_the_invite_to_everyone_else(self):
        with mock.patch.object(bot.DB, "update_and_check", return_value=False), \
             mock.patch.object(bot, "is_subscribed", return_value=False), \
             mock.patch.object(bot, "tg_request") as send:
            self.assertFalse(bot.ensure_access(STRANGER, "u", "token", ADMIN))
        send.assert_called_once()


class PhotoIsAnswered(unittest.TestCase):
    def test_photo_gets_a_reply_instead_of_silence(self):
        with mock.patch.object(bot, "ensure_access", return_value=True), \
             mock.patch.object(bot, "DB") as db, \
             mock.patch.object(bot, "capabilities_for_model", return_value=[]), \
             mock.patch.object(bot, "tg_send_text") as say:
            db.get_session.return_value = {"provider": "groq", "model": "m", "ui_lang": "ru"}
            bot.process_update(photo_update(ADMIN), "token", ADMIN)
        say.assert_called_once()


class Feedback(unittest.TestCase):
    def test_feedback_reaches_the_admin(self):
        with mock.patch.object(bot, "DB") as db, mock.patch.object(bot, "tg_send_text") as say:
            db.get_session.return_value = {"ui_lang": "ru"}
            bot.handle_command(STRANGER, "u", "/feedback кнопки не видно", "token", ADMIN)
        self.assertIn(ADMIN, [c.args[1] for c in say.call_args_list])

    def test_empty_feedback_asks_for_text(self):
        with mock.patch.object(bot, "DB") as db, mock.patch.object(bot, "tg_send_text") as say:
            db.get_session.return_value = {"ui_lang": "ru"}
            bot.handle_command(STRANGER, "u", "/feedback", "token", ADMIN)
        self.assertEqual([c.args[1] for c in say.call_args_list], [STRANGER])


class ProviderHealth(unittest.TestCase):
    def test_a_fully_dead_provider_is_called_out(self):
        rows = [{"provider": "huggingface", "total": 130, "live": 0, "last_check": 0},
                {"provider": "groq", "total": 8, "live": 6, "last_check": 0}]
        with mock.patch.object(bot.DB, "get_provider_health", return_value=rows):
            txt = bot.build_provider_health_text()
        self.assertIn("Полностью мёртвые: huggingface", txt)

    def test_retired_provider_rows_are_not_reported(self):
        with mock.patch.object(bot.DB, "get_provider_health", return_value=[]):
            self.assertEqual(bot.build_provider_health_text(), "Нет данных о провайдерах.")

    def test_healthy_fleet_has_no_warning(self):
        rows = [{"provider": "groq", "total": 8, "live": 6, "last_check": 0}]
        with mock.patch.object(bot.DB, "get_provider_health", return_value=rows):
            self.assertNotIn("мёртвые", bot.build_provider_health_text())


if __name__ == "__main__":
    unittest.main()
