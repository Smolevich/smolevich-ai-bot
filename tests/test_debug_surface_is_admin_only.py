"""`sid: 8f3a…`, `In: 412 | Out: 88 | Ctx: 1200/64000`, миллисекунды — это админский слой.

2026-09-07: отладочный подвал и экран статуса зависели только от того, лежит ли uid
в DEBUG_USERS, а не от того, админ ли он. Человеку эти цифры не говорят ничего, кроме
«тут что-то техническое, наверное я что-то сломал».

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

_spec = importlib.util.spec_from_file_location("bot_debug_surface", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

ADMIN = 1
USER = 7
SESSION = {"provider": "groq", "model": "llama-3.3-70b-versatile", "ui_lang": "ru",
           "tools_enabled": True, "history": [], "engine_mode": "native"}


class StatusScreen(unittest.TestCase):
    def sent(self, uid):
        out = []
        with mock.patch.object(bot, "tg_send_text", side_effect=lambda t, u, text, **k: out.append(text) or {"ok": True}), \
             mock.patch.object(bot.DB, "get_session", return_value=dict(SESSION)), \
             mock.patch.object(bot.DB, "set_last_session_id"), \
             mock.patch.object(bot.DB, "get_model_info", return_value=None), \
             mock.patch.object(bot, "fetch_openrouter_key_limits", return_value={}), \
             mock.patch.object(bot, "load_provider_key", return_value="k"):
            bot.send_status_text("token", uid, ADMIN)
        return out

    def test_a_non_admin_is_shown_no_status_at_all(self):
        self.assertEqual(self.sent(USER), [])

    def test_the_admin_still_gets_the_status(self):
        self.assertTrue(self.sent(ADMIN))


class DebugFooter(unittest.TestCase):
    def setUp(self):
        with bot.DEBUG_USERS_LOCK:
            bot.DEBUG_USERS.clear()

    def test_a_non_admin_never_gets_the_footer(self):
        """Даже если его id как-то попал в DEBUG_USERS — подвал админский."""
        with bot.DEBUG_USERS_LOCK:
            bot.DEBUG_USERS.add(USER)
        self.assertFalse(bot.should_show_debug_footer(USER, ADMIN))

    def test_the_admin_who_turned_it_on_gets_the_footer(self):
        with bot.DEBUG_USERS_LOCK:
            bot.DEBUG_USERS.add(ADMIN)
        self.assertTrue(bot.should_show_debug_footer(ADMIN, ADMIN))

    def test_the_admin_who_left_it_off_gets_no_footer(self):
        self.assertFalse(bot.should_show_debug_footer(ADMIN, ADMIN))


if __name__ == "__main__":
    unittest.main()
