"""Bottom keyboard: at most four buttons, and its labels must not reach the model.

Also locks in the TTS fix: the mode flag was set by the menu but never read, so
"send text and I'll return audio" answered with plain text instead.

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

_spec = importlib.util.spec_from_file_location("bot_quick", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

SESSION_RU = {"provider": "groq", "model": "llama-3.3-70b-versatile", "ui_lang": "ru"}
SESSION_EN = dict(SESSION_RU, ui_lang="en")


def keyboard(sess, stt=True, tts=True):
    with mock.patch.object(bot, "has_stt_models", return_value=stt), \
         mock.patch.object(bot, "has_tts_models", return_value=tts):
        return bot.build_quick_keyboard(sess)


def buttons(kb):
    return [b["text"] for row in kb["keyboard"] for b in row]


class QuickKeyboard(unittest.TestCase):
    def test_never_more_than_four_buttons(self):
        self.assertLessEqual(len(buttons(keyboard(SESSION_RU))), 4)

    def test_voice_buttons_hidden_when_unavailable(self):
        self.assertEqual(len(buttons(keyboard(SESSION_RU, stt=False, tts=False))), 2)

    def test_english_session_gets_english_labels(self):
        self.assertIn(bot.QUICK_MORE["en"], buttons(keyboard(SESSION_EN)))


class ChatIsAlwaysReachable(unittest.TestCase):
    def test_chat_button_is_always_there(self):
        self.assertIn(bot.QUICK_CHAT["ru"], buttons(keyboard(SESSION_RU, stt=False, tts=False)))

    def test_chat_leaves_the_voice_mode(self):
        with bot.pendingTtsUsersLock:
            bot.pendingTtsUsers.add(7)
        with mock.patch.object(bot, "DB") as db, mock.patch.object(bot, "tg_send_text"):
            db.get_session.return_value = SESSION_RU
            bot.handle_quick_action("chat", 7, "token", admin_id=0)
        self.assertFalse(bot.take_pending_tts(7))


class MenuRootText(unittest.TestCase):
    def test_no_explanatory_blurb(self):
        with mock.patch.object(bot, "has_video_detector", return_value=False):
            txt, _ = bot.build_menu_root(SESSION_RU, is_admin=False)
        self.assertEqual(txt, "☰ Ещё")


class QuickLabelRouting(unittest.TestCase):
    def test_label_is_recognised_in_both_languages(self):
        self.assertEqual(bot.quick_action_for(bot.QUICK_STT["ru"]), "stt")
        self.assertEqual(bot.quick_action_for(bot.QUICK_STT["en"]), "stt")

    def test_ordinary_text_is_not_a_button(self):
        self.assertEqual(bot.quick_action_for("расскажи про Белград"), "")

    def test_every_button_label_routes_somewhere(self):
        for label in buttons(keyboard(SESSION_RU)):
            self.assertNotEqual(bot.quick_action_for(label), "", label)


class PendingTts(unittest.TestCase):
    def setUp(self):
        with bot.pendingTtsUsersLock:
            bot.pendingTtsUsers.clear()

    def test_text_after_tts_button_is_voiced(self):
        with bot.pendingTtsUsersLock:
            bot.pendingTtsUsers.add(42)
        self.assertTrue(bot.take_pending_tts(42))

    def test_flag_is_one_shot(self):
        with bot.pendingTtsUsersLock:
            bot.pendingTtsUsers.add(42)
        bot.take_pending_tts(42)
        self.assertFalse(bot.take_pending_tts(42))

    def test_plain_user_is_never_voiced(self):
        self.assertFalse(bot.take_pending_tts(99))


if __name__ == "__main__":
    unittest.main()
