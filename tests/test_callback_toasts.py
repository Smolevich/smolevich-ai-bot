"""Всплывашка от кнопки — тоже текст бота, и она была на английском и про модели.

2026-09-07: нажатие в меню отвечало `Model updated`, `Failed to update model`,
`Model already selected`, `STT сейчас недоступен`. Человек не знает слов «модель»
и «провайдер», и по-английски с ним никто не договаривался.

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

_spec = importlib.util.spec_from_file_location("bot_toasts", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

ADMIN = 1
USER = 7
SESSION = {"provider": "groq", "model": "llama-3.3-70b-versatile", "ui_lang": "ru",
           "tools_enabled": True, "history": [], "engine_mode": "native"}

# Everything a non-admin can press: what the current keyboards draw, plus the routes
# that live on in clients whose keyboard is months old.
NON_ADMIN_ROUTES = [
    "menu:back", "menu:curious", "menu:settings", "menu:lang_toggle", "menu:reset",
    "menu:help", "menu:model", "menu:chat", "menu:stt", "menu:tts", "menu:video",
    "menu:top", "menu:provider", "menu:admin", "menu:code", "menu:mode", "menu:tools",
    "menu:status", "menu:debug", "menu:users",
    "set_model:qwen/qwen3.8-27b", "set_provider:nvidia", "try:g:qwen/qwen3.8-27b",
    "set_mode:claude", "set_tools:on", "set_debug:on", "reset_context",
]


def toasts_for(route, uid=USER, edit_ok=True, features=True):
    """Every `answerCallbackQuery` text one press produces."""
    said = []

    def fake_request(_token, method, payload=None):
        if method == "answerCallbackQuery":
            said.append(payload.get("text", ""))
            return {"ok": True}
        if method == "editMessageText" and not edit_ok:
            return {"ok": False, "description": "Bad Request: message is not modified"}
        return {"ok": True}

    cb = {"id": "1", "from": {"id": uid}, "data": route,
          "message": {"chat": {"id": uid}, "message_id": 42}}
    with mock.patch.object(bot, "tg_request", side_effect=fake_request), \
         mock.patch.object(bot, "tg_send_text"), \
         mock.patch.object(bot.DB, "get_session", return_value=dict(SESSION)), \
         mock.patch.object(bot.DB, "save_session"), \
         mock.patch.object(bot.DB, "set_last_session_id"), \
         mock.patch.object(bot.DB, "get_model_info", return_value=None), \
         mock.patch.object(bot.DB, "get_recent_models", return_value=[]), \
         mock.patch.object(bot.DB, "get_healthy_models", return_value=[]), \
         mock.patch.object(bot.DB, "pick_default_text_model", return_value="a/b"), \
         mock.patch.object(bot.DB, "log_ui_event"), \
         mock.patch.object(bot, "live_model_ranking", return_value=[("groq", "a/b")]), \
         mock.patch.object(bot, "has_stt_models", return_value=features), \
         mock.patch.object(bot, "has_tts_models", return_value=features), \
         mock.patch.object(bot, "pick_video_detector", return_value=("nvidia", "d") if features else (None, None)), \
         mock.patch.object(bot, "available_providers", return_value=["groq"]), \
         mock.patch.object(bot.os.path, "exists", return_value=False):
        bot.handle_callback(cb, "token", admin_id=ADMIN)
    return said


def every_non_admin_toast():
    out = []
    for route in NON_ADMIN_ROUTES:
        for features in (True, False):
            out.extend((route, t) for t in toasts_for(route, features=features))
    out.extend((route, t) for route in ("set_model:a/b",) for t in toasts_for(route, edit_ok=False))
    return out


def has_cyrillic(text):
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in text)


class WordsAPersonNeverLearned(unittest.TestCase):
    def test_no_toast_a_non_admin_can_see_says_model(self):
        for route, text in every_non_admin_toast():
            with self.subTest(route=route):
                self.assertNotIn("model", text.lower())

    def test_no_toast_a_non_admin_can_see_says_модель(self):
        for route, text in every_non_admin_toast():
            with self.subTest(route=route):
                self.assertNotIn("модел", text.lower())

    def test_no_toast_a_non_admin_can_see_says_provider(self):
        for route, text in every_non_admin_toast():
            with self.subTest(route=route):
                self.assertNotIn("provider", text.lower())
                self.assertNotIn("провайдер", text.lower())

    def test_no_toast_a_non_admin_can_see_says_limit(self):
        for route, text in every_non_admin_toast():
            with self.subTest(route=route):
                self.assertNotIn("лимит", text.lower())


class SpokenInRussian(unittest.TestCase):
    def test_every_toast_a_non_admin_can_see_is_russian(self):
        """Пусто или эмодзи — можно; латинская фраза — нет.

        Переключатель языка исключён: он отвечает на языке, который только что выбрали.
        """
        for route, text in every_non_admin_toast():
            if route == "menu:lang_toggle":
                continue
            with self.subTest(route=route, text=text):
                if any(ch.isalpha() for ch in text):
                    self.assertTrue(has_cyrillic(text), text)

    def test_the_language_toast_answers_in_the_language_just_chosen(self):
        self.assertEqual(toasts_for("menu:lang_toggle"), ["Language: EN"])


class EveryPressIsAnswered(unittest.TestCase):
    """Кнопка без ветки в маршрутизаторе — это крутящийся спиннер до таймаута Telegram."""

    def test_a_stale_admin_button_does_not_spin(self):
        self.assertTrue(toasts_for("menu:admin"))

    def test_an_unknown_menu_route_does_not_spin(self):
        self.assertEqual(len(toasts_for("menu:whatever-we-removed")), 1)

    def test_an_unknown_callback_altogether_does_not_spin(self):
        self.assertEqual(len(toasts_for("some_button_from_2024:1")), 1)


class ParticularWordings(unittest.TestCase):
    def test_a_switch_that_did_not_apply_says_so_in_russian(self):
        said = toasts_for("set_model:a/b", edit_ok=False)
        self.assertTrue(any(has_cyrillic(t) for t in said), said)

    def test_pressing_the_model_already_chosen_is_not_an_error(self):
        self.assertNotIn("❌", "".join(toasts_for("set_model:a/b", edit_ok=False)))


if __name__ == "__main__":
    unittest.main()
