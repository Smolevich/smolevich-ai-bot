"""«Провайдер» — это не понятие пользователя, поэтому экран у него не открывается.

2026-09-07: `menu:provider` рисовал голые `openrouter` / `groq` / `nvidia`, а
`set_provider:` менял по ним ответчика. Слово «провайдер» человеку ничего не
объясняет и решать ему нечего — бот выбирает сам. Админу экран нужен, но именами,
а не идентификаторами.

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

_spec = importlib.util.spec_from_file_location("bot_provider_screen", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

ADMIN = 1
USER = 7
SESSION = {"provider": "groq", "model": "llama-3.3-70b-versatile", "ui_lang": "ru",
           "tools_enabled": True, "history": [], "engine_mode": "native"}


def press(data, uid, admin_id):
    """Run one inline press and return every Telegram call it made."""
    calls = []
    cb = {"id": "1", "from": {"id": uid}, "data": data,
          "message": {"chat": {"id": uid}, "message_id": 42}}
    with mock.patch.object(bot, "tg_request", side_effect=lambda t, m, p=None: calls.append((m, p)) or {"ok": True}), \
         mock.patch.object(bot.DB, "get_session", return_value=dict(SESSION)), \
         mock.patch.object(bot.DB, "save_session"), \
         mock.patch.object(bot.DB, "pick_default_text_model", return_value="llama-3.3-70b-versatile"), \
         mock.patch.object(bot.DB, "log_ui_event"), \
         mock.patch.object(bot, "available_providers", return_value=["groq", "openrouter", "nvidia"]):
        bot.handle_callback(cb, "token", admin_id=admin_id)
    return calls


def screens(calls):
    return [p for m, p in calls if m in ("editMessageText", "sendMessage")]


def manual_screen(is_admin):
    """«🤖 Выбрать вручную», с обоими источниками данных под моками."""
    with mock.patch.object(bot.DB, "get_recent_models", return_value=[
             {"id": "a/b", "latency_ms": 10, "available": True, "supportsTools": True}]), \
         mock.patch.object(bot.DB, "get_healthy_models", return_value=[]), \
         mock.patch.object(bot.DB, "get_model_info", return_value=None):
        return bot.build_models_view(SESSION, is_admin=is_admin)


class NonAdmin(unittest.TestCase):
    def test_non_admin_is_not_shown_the_provider_screen(self):
        self.assertEqual(screens(press("menu:provider", USER, ADMIN)), [])

    def test_non_admin_cannot_switch_provider_by_an_old_button(self):
        """Кнопка живёт в клиенте дольше, чем в коде: старое меню не должно менять ответчика."""
        self.assertEqual(screens(press("set_provider:nvidia", USER, ADMIN)), [])

    def test_non_admin_manual_screen_has_no_way_into_providers(self):
        _, kb = manual_screen(is_admin=False)
        routes = [b["callback_data"] for row in kb for b in row]
        self.assertNotIn("menu:provider", routes)


class Admin(unittest.TestCase):
    def test_admin_still_gets_the_provider_screen(self):
        self.assertTrue(screens(press("menu:provider", ADMIN, ADMIN)))

    def test_admin_sees_names_not_identifiers(self):
        kb = screens(press("menu:provider", ADMIN, ADMIN))[0]["reply_markup"]["inline_keyboard"]
        labels = [b["text"] for row in kb for b in row]
        self.assertIn("NVIDIA", " ".join(labels))

    def test_admin_manual_screen_keeps_the_provider_row(self):
        _, kb = manual_screen(is_admin=True)
        routes = [b["callback_data"] for row in kb for b in row]
        self.assertIn("menu:provider", routes)


if __name__ == "__main__":
    unittest.main()
