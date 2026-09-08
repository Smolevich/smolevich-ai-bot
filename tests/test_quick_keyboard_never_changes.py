"""Regression: the row of buttons under the chat is the same one everywhere, always.

«Быстрые кнопки меняются, а они не должны меняться» — 2026-09-08. The layout was built
from `has_stt_models()`, so the transcription button appeared and vanished on its own
whenever the health probe changed its mind, and the person saw a keyboard reshuffle
after doing nothing. A feature that is down says so when its button is pressed; it does
not take the button away.

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

_spec = importlib.util.spec_from_file_location("bot_kb_uniform", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

ADMIN = 1
USER = 7
SESSION = {"provider": "openrouter", "model": "a/b", "ui_lang": "ru", "tools_enabled": True,
           "history": [], "engine_mode": "native", "model_pinned": False, "last_session_id": ""}

MENU_ROUTES = ["back", "curious", "settings", "admin", "chat", "code", "voice", "stt", "tts",
               "video", "model", "provider", "status", "help", "top", "mode", "tools",
               "debug", "users", "reset", "lang_toggle"]
QUICK_ACTIONS = ["chat", "stt", "tts", "board", "model", "more"]


def keyboard_of(uid, lang="ru"):
    with mock.patch.object(bot.DB, "get_session", return_value=dict(SESSION, ui_lang=lang)):
        return bot.quick_keyboard(uid)


def markups_from(run):
    """Every reply_markup the bot sent while `run` was executing."""
    seen = []

    def record(_token, method, payload=None, **_kw):
        if method in ("sendMessage", "editMessageText") and payload:
            seen.append(payload.get("reply_markup"))
        return {"ok": True, "result": {"message_id": 1}}

    def record_text(_token, _uid, _text, **kw):
        seen.append(kw.get("reply_markup"))
        return {"ok": True}

    with mock.patch.object(bot, "tg_request", side_effect=record), \
         mock.patch.object(bot, "tg_send_text", side_effect=record_text), \
         mock.patch.object(bot, "tg_send_long_text", side_effect=record_text), \
         mock.patch.object(bot.DB, "get_session", return_value=dict(SESSION)), \
         mock.patch.object(bot.DB, "save_session"), \
         mock.patch.object(bot.DB, "log_ui_event"), \
         mock.patch.object(bot.DB, "set_last_session_id"), \
         mock.patch.object(bot.DB, "get_all_users_stats", return_value=[]), \
         mock.patch.object(bot.DB, "get_recent_models", return_value=[]), \
         mock.patch.object(bot.DB, "get_healthy_models", return_value=[]), \
         mock.patch.object(bot, "live_model_ranking", return_value=[("openrouter", "a/b")]), \
         mock.patch.object(bot, "live_pairs", return_value={("openrouter", "a/b")}), \
         mock.patch.object(bot, "claude_cli_model_now", return_value=("openrouter", "a/b")), \
         mock.patch.object(bot, "available_providers", return_value=["openrouter"]), \
         mock.patch.object(bot, "send_status_text"), \
         mock.patch.object(bot, "build_provider_health_text", return_value="health"), \
         mock.patch.object(bot, "build_board_admin_text", return_value="board"), \
         mock.patch.object(bot, "pick_video_detector", return_value=("openrouter", "v/m")), \
         mock.patch.object(bot, "ensure_text_model_for_session", return_value=("openrouter", "a/b", False)):
        run()
    return seen


def is_uniform(markup, uid):
    """A message may carry no reply keyboard, an inline one, or the one quick keyboard."""
    if not markup:
        return True
    if "inline_keyboard" in markup:
        return True
    return markup == keyboard_of(uid)


class EveryRouteShowsTheSameKeyboard(unittest.TestCase):
    def check(self, uid, run):
        for markup in markups_from(run):
            self.assertTrue(is_uniform(markup, uid), markup)

    def test_start_and_menu(self):
        for uid in (ADMIN, USER):
            for cmd in ("/start", "/menu"):
                with self.subTest(uid=uid, cmd=cmd):
                    self.check(uid, lambda u=uid, c=cmd: bot.handle_command(u, "u", c, "token", ADMIN))

    def test_every_menu_callback(self):
        for uid in (ADMIN, USER):
            for action in MENU_ROUTES:
                with self.subTest(uid=uid, action=action):
                    cb = {"id": "1", "from": {"id": uid}, "data": f"menu:{action}",
                          "message": {"chat": {"id": uid}, "message_id": 2}}
                    self.check(uid, lambda c=cb: bot.handle_callback(c, "token", ADMIN))

    def test_every_quick_action(self):
        for uid in (ADMIN, USER):
            for action in QUICK_ACTIONS:
                with self.subTest(uid=uid, action=action):
                    self.check(uid, lambda u=uid, a=action: bot.handle_quick_action(a, u, "token", ADMIN))

    def test_the_welcome_after_the_gate(self):
        for uid in (ADMIN, USER):
            with self.subTest(uid=uid):
                self.check(uid, lambda u=uid: bot.welcome_after_gate(u, "token", ADMIN))


class TheLayoutDoesNotDependOnTheWeather(unittest.TestCase):
    def rows(self, **health):
        defaults = {"has_stt_models": True, "has_tts_models": True, "has_video_detector": True}
        defaults.update(health)
        with mock.patch.object(bot, "has_stt_models", return_value=defaults["has_stt_models"]), \
             mock.patch.object(bot, "has_tts_models", return_value=defaults["has_tts_models"]), \
             mock.patch.object(bot, "has_video_detector", return_value=defaults["has_video_detector"]):
            return keyboard_of(USER)

    def test_a_dead_transcription_probe_does_not_remove_a_button(self):
        self.assertEqual(self.rows(has_stt_models=False), self.rows(has_stt_models=True))

    def test_nothing_healthy_at_all_still_gives_the_same_keyboard(self):
        self.assertEqual(
            self.rows(has_stt_models=False, has_tts_models=False, has_video_detector=False),
            self.rows())

    def test_the_admin_sees_what_everyone_sees(self):
        self.assertEqual(keyboard_of(ADMIN), keyboard_of(USER))

    def test_it_stays_within_the_four_button_cap(self):
        buttons = [b for row in keyboard_of(USER)["keyboard"] for b in row]
        self.assertLessEqual(len(buttons), 4)

    def test_every_label_still_routes_somewhere(self):
        for row in keyboard_of(USER)["keyboard"]:
            for button in row:
                self.assertNotEqual(bot.quick_action_for(button["text"]), "", button)

    def test_an_english_session_gets_english_labels(self):
        self.assertIn(bot.QUICK_MORE["en"],
                      [b["text"] for row in keyboard_of(USER, lang="en")["keyboard"] for b in row])


class NothingEverRemovesTheKeyboard(unittest.TestCase):
    def test_the_source_has_no_reply_keyboard_remove(self):
        source = (_BOT_DIR / "smolevich-ai-bot.py").read_text(encoding="utf-8")
        self.assertNotIn("remove_keyboard", source)

    def test_only_one_function_builds_it(self):
        source = (_BOT_DIR / "smolevich-ai-bot.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('"keyboard":'), 1)


if __name__ == "__main__":
    unittest.main()
