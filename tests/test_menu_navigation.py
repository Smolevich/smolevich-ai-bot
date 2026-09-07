"""Regression: every submenu has a way back, and a button press leaves no text behind.

2026-09-07: the Provider screen had no "← Назад" — the only exit was to abandon the
menu. And "☰ More" appeared in the chat twice, once as the person's tap (a reply
keyboard cannot be pressed without sending its label) and once as the bot's own message,
because the root menu's title was that same string.

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

_spec = importlib.util.spec_from_file_location("bot_navigation", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

SESSION = {"provider": "groq", "model": "llama-3.3-70b-versatile", "ui_lang": "ru",
           "tools_enabled": True, "history": [], "engine_mode": "native"}


def provider_screen_keyboard():
    """The keyboard the `menu:provider` branch builds, taken from the request it sends."""
    sent = []
    cb = {"id": "1", "from": {"id": 7}, "data": "menu:provider",
          "message": {"chat": {"id": 7}, "message_id": 42}}
    with mock.patch.object(bot, "tg_request", side_effect=lambda t, m, p=None: sent.append((m, p)) or {"ok": True}), \
         mock.patch.object(bot.DB, "get_session", return_value=dict(SESSION)), \
         mock.patch.object(bot.DB, "log_ui_event"), \
         mock.patch.object(bot, "available_providers", return_value=["groq", "openrouter"]):
        bot.handle_callback(cb, "token", admin_id=0)
    for method, payload in sent:
        if method == "editMessageText":
            return payload["reply_markup"]["inline_keyboard"]
    raise AssertionError(f"no screen was drawn: {sent}")


def submenu_keyboards():
    """Every screen reachable below the root, by the builder that draws it."""
    with mock.patch.object(bot, "has_stt_models", return_value=True), \
         mock.patch.object(bot, "has_tts_models", return_value=True), \
         mock.patch.object(bot, "has_video_detector", return_value=True), \
         mock.patch.object(bot.DB, "get_recent_models", return_value=[
             {"id": "a/b", "latency_ms": 10, "available": True, "supportsTools": True}]), \
         mock.patch.object(bot.DB, "get_healthy_models", return_value=[]), \
         mock.patch.object(bot, "live_model_ranking", return_value=[("groq", "a/b")]):
        return {
            "menu:settings": bot.build_menu_settings(SESSION)[1],
            "menu:model": bot.build_models_view(SESSION)[1],
            "menu:curious": bot.build_curious_view(SESSION)[1],
            "menu:admin": bot.build_admin_menu(SESSION)[1],
            "menu:provider": provider_screen_keyboard(),
        }


class EveryScreenHasAWayBack(unittest.TestCase):
    def test_the_provider_screen_has_a_back_button(self):
        kb = provider_screen_keyboard()
        self.assertTrue(kb[-1][0]["text"].startswith("←"), kb)

    def test_no_submenu_anywhere_is_a_dead_end(self):
        for route, kb in submenu_keyboards().items():
            with self.subTest(route=route):
                self.assertTrue(kb[-1][0]["text"].startswith("←"), f"{route}: {kb}")

    def test_the_back_row_is_last_and_alone_on_every_screen(self):
        """Same button, same place, every screen — that is the whole rule."""
        for route, kb in submenu_keyboards().items():
            with self.subTest(route=route):
                self.assertEqual(len(kb[-1]), 1, f"{route}: {kb[-1]}")

    def test_back_leads_to_a_screen_that_exists(self):
        known = {"menu:back", "menu:curious", "menu:model", "menu:settings"}
        for route, kb in submenu_keyboards().items():
            with self.subTest(route=route):
                self.assertIn(kb[-1][0]["callback_data"], known, route)


class PressingAButtonLeavesNoText(unittest.TestCase):
    def test_the_tap_that_opened_the_menu_is_deleted(self):
        calls = []
        with mock.patch.object(bot, "tg_request", side_effect=lambda t, m, p=None: calls.append((m, p)) or {"ok": True}), \
             mock.patch.object(bot.DB, "get_session", return_value=dict(SESSION)), \
             mock.patch.object(bot.DB, "log_ui_event"), \
             mock.patch.object(bot, "has_tts_models", return_value=False), \
             mock.patch.object(bot, "has_video_detector", return_value=False):
            bot.handle_quick_action("more", 7, "token", admin_id=0, message_id=555)
        deletes = [p for m, p in calls if m == "deleteMessage"]
        self.assertEqual(deletes, [{"chat_id": 7, "message_id": 555}])

    def test_the_bot_never_sends_the_button_label_back(self):
        calls = []
        with mock.patch.object(bot, "tg_request", side_effect=lambda t, m, p=None: calls.append((m, p)) or {"ok": True}), \
             mock.patch.object(bot.DB, "get_session", return_value=dict(SESSION)), \
             mock.patch.object(bot.DB, "log_ui_event"), \
             mock.patch.object(bot, "has_tts_models", return_value=False), \
             mock.patch.object(bot, "has_video_detector", return_value=False):
            bot.handle_quick_action("more", 7, "token", admin_id=0, message_id=555)
        labels = set(bot.QUICK_MORE.values())
        for method, payload in calls:
            if method == "sendMessage":
                self.assertNotIn(payload.get("text"), labels, payload)


if __name__ == "__main__":
    unittest.main()
