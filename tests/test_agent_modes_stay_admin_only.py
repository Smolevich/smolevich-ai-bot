"""«Режим claude работает только на openrouter» и «песочница» — не для обычного человека.

Сессия могла остаться в агентном режиме (например, после админского теста на своём же
id), и тогда человек получал служебную заметку про режимы и провайдеров. Ветку уже
переводят в native, но и заметка не должна уходить никому, кроме админа.

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

_spec = importlib.util.spec_from_file_location("bot_agent_modes", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

ADMIN = 1
USER = 7
CLAUDE_SESSION = {"provider": "nvidia", "model": "m", "ui_lang": "ru", "tools_enabled": True,
                  "history": [], "engine_mode": "claude", "model_pinned": False}

_next_update_id = iter(range(50_000, 60_000))


def ask(uid):
    """Задать вопрос сессией, застрявшей в агентном режиме; вернуть всё сказанное."""
    said = []
    with mock.patch.object(bot, "ensure_access", return_value=True), \
         mock.patch.object(bot, "tg_send_text", side_effect=lambda t, u, text, **k: said.append(text) or {"ok": True}), \
         mock.patch.object(bot, "tg_send_long_text", side_effect=lambda t, u, text, **k: said.append(text) or {"ok": True}), \
         mock.patch.object(bot, "tg_request", return_value={"ok": True}), \
         mock.patch.object(bot, "capabilities_for_model", return_value=["text"]), \
         mock.patch.object(bot, "keep_typing", return_value=lambda: None), \
         mock.patch.object(bot, "load_provider_key", return_value="k"), \
         mock.patch.object(bot, "ask_via_acpx", return_value=("из песочницы", {"prompt_tokens": 1, "completion_tokens": 1}, {"finish_reason": "stop", "tool_calls_total": 0, "error": None, "session_id": "s"})), \
         mock.patch.object(bot, "answer_with_fallback",
                           return_value=("ответ", {"prompt_tokens": 1, "completion_tokens": 1},
                                         {"finish_reason": "stop", "tool_calls_total": 0, "error": None},
                                         "groq", "m")), \
         mock.patch.object(bot, "ensure_text_model_for_session", return_value=("groq", "m", False)), \
         mock.patch.object(bot.DB, "get_session", return_value=dict(CLAUDE_SESSION)), \
         mock.patch.object(bot.DB, "save_session"), \
         mock.patch.object(bot.DB, "add_usage"), \
         mock.patch.object(bot.DB, "log_request", return_value=1), \
         mock.patch.object(bot.DB, "set_request_delivered"), \
         mock.patch.object(bot.DB, "set_last_session_id"):
        bot.process_update({"update_id": next(_next_update_id),
                            "message": {"message_id": 3, "from": {"id": uid, "username": "u"},
                                        "chat": {"id": uid}, "text": "сколько будет два плюс два"}},
                           "token", ADMIN)
    return " ".join(said)


class NonAdmin(unittest.TestCase):
    def test_a_stale_agent_session_never_explains_engine_modes(self):
        self.assertNotIn("Режим", ask(USER))

    def test_a_stale_agent_session_never_names_a_provider(self):
        self.assertNotIn("openrouter", ask(USER).lower())

    def test_a_stale_agent_session_never_mentions_the_sandbox(self):
        self.assertNotIn("песочниц", ask(USER))


class Admin(unittest.TestCase):
    def test_the_admin_gets_the_answer_and_not_a_note_about_engines(self):
        """The «Режим claude работает только на openrouter» notice is gone for good.

        Which engine answered and on what model is routing, not conversation; the admin
        already has it in the debug footer and in request_log.
        """
        with mock.patch.object(bot, "harness_target", return_value=("openrouter", "picked/model", True)):
            said = ask(ADMIN)
        self.assertNotIn("Режим", said)

    def test_the_admin_still_gets_an_answer(self):
        with mock.patch.object(bot, "harness_target", return_value=("openrouter", "picked/model", True)):
            self.assertIn("ответ", ask(ADMIN))


if __name__ == "__main__":
    unittest.main()
