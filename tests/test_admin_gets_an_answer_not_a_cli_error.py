"""Regression: the sandbox failing is not something a person reads about.

2026-09-08, the admin wrote «Даров» and got back, verbatim:

    Не справился с задачей.
    Internal error: There's an issue with the selected model
    (minimax/minimax-m3:free[1m]). It may not exist or you may not have access to it.
    Run --model to pick a different model.

Three separate faults in one message: the admin's session was answering through the
claude CLI, the CLI could not resolve the model, and the CLI's own words went into the
chat instead of an answer. Nothing was retried, though two live models were sitting there.

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

_spec = importlib.util.spec_from_file_location("bot_admin_answer", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

ADMIN = 153315638
PLAIN_USER = 7
CLI_ERROR = ("Internal error: There's an issue with the selected model "
             "(minimax/minimax-m3:free[1m]). It may not exist or you may not have access "
             "to it. Run --model to pick a different model.")
WHITELISTED = bot.model_routing.CLAUDE_CLI_MODELS[0]
LIVE = {WHITELISTED, ("openrouter", "minimax/minimax-m3:free")}
CLAUDE_SESSION = {"provider": "openrouter", "model": "minimax/minimax-m3:free", "ui_lang": "ru",
                  "tools_enabled": True, "history": [], "engine_mode": "claude",
                  "model_pinned": True, "last_session_id": ""}


def answer(acpx_result, session=None, uid=ADMIN):
    """Ask through the single door with the sandbox scripted to a given outcome."""
    with mock.patch.object(bot, "live_pairs", return_value=set(LIVE)), \
         mock.patch.object(bot, "ask_via_acpx", return_value=acpx_result), \
         mock.patch.object(bot, "live_model_ranking", return_value=[("openrouter", "good/model")]), \
         mock.patch.object(bot, "ask_llm", return_value=("нативный ответ", {"prompt_tokens": 1, "completion_tokens": 1},
                                                        {"finish_reason": "stop", "tool_calls_total": 0, "error": None,
                                                         "http_latency_ms": 1, "rate_limits": {}, "status": None,
                                                         "retry_after_sec": None})), \
         mock.patch.object(bot, "load_provider_key", return_value="k"), \
         mock.patch.object(bot, "capabilities_for_model", return_value=["text"]), \
         mock.patch.object(bot.DB, "log_request"):
        return bot.answer_with_fallback(uid, ADMIN, dict(session or CLAUDE_SESSION), [], "Даров", "sys")


def sandbox_failed():
    return None, {"prompt_tokens": 0, "completion_tokens": 0}, {
        "finish_reason": "acpx_error", "tool_calls_total": 0, "error": CLI_ERROR[:200],
        "session_id": "s", "http_latency_ms": 0, "rate_limits": {}, "status": None,
        "retry_after_sec": None}


class SandboxFailure(unittest.TestCase):
    def test_the_cli_error_is_never_the_answer(self):
        ans, _, _, _, _ = answer(sandbox_failed())
        self.assertNotIn("Internal error", ans)

    def test_a_failed_sandbox_is_answered_natively_instead(self):
        ans, _, _, _, _ = answer(sandbox_failed())
        self.assertEqual(ans, "нативный ответ")

    def test_the_mode_that_actually_answered_is_reported(self):
        _, _, meta, _, _ = answer(sandbox_failed())
        self.assertEqual(meta["mode"], "native")

    def test_a_busy_sandbox_lock_also_falls_through(self):
        busy = bot.acpx_failure(ADMIN, "acpx_busy", "lock_busy", "s")
        self.assertEqual(answer(busy)[0], "нативный ответ")


class SandboxAnswer(unittest.TestCase):
    def test_a_working_sandbox_still_answers(self):
        ok = ("из песочницы", {"prompt_tokens": 0, "completion_tokens": 0},
              {"finish_reason": "acpx_claude", "tool_calls_total": 0, "error": None, "session_id": "s"})
        self.assertEqual(answer(ok)[0], "из песочницы")

    def test_the_sandbox_runs_on_a_whitelisted_model_not_the_pinned_one(self):
        ok = ("из песочницы", {"prompt_tokens": 0, "completion_tokens": 0},
              {"finish_reason": "acpx_claude", "tool_calls_total": 0, "error": None, "session_id": "s"})
        _, _, _, provider, model = answer(ok)
        self.assertEqual((provider, model), WHITELISTED)


class ReasoningInTheSandboxBranch(unittest.TestCase):
    def test_thinking_from_the_cli_never_reaches_the_person(self):
        """«[thinking] The user greeted me in Russian…» arrived in front of «Привет!»."""
        leaky = ("[thinking] The user greeted me in Russian, this is a casual greeting.\n"
                 "Привет! Чем могу помочь?",
                 {"prompt_tokens": 0, "completion_tokens": 0},
                 {"finish_reason": "acpx_claude", "tool_calls_total": 0, "error": None, "session_id": "s"})
        self.assertEqual(answer(leaky)[0], "Привет! Чем могу помочь?")


class DeadPairing(unittest.TestCase):
    def test_claude_mode_with_no_verified_model_live_answers_natively(self):
        with mock.patch.object(bot.model_routing, "CLAUDE_CLI_MODELS", ()):
            self.assertEqual(answer(sandbox_failed())[0], "нативный ответ")

    def test_a_non_admin_never_enters_the_sandbox(self):
        called = []
        with mock.patch.object(bot, "live_pairs", return_value=set(LIVE)), \
             mock.patch.object(bot, "ask_via_acpx", side_effect=lambda *a, **k: called.append(1)), \
             mock.patch.object(bot, "live_model_ranking", return_value=[("openrouter", "good/model")]), \
             mock.patch.object(bot, "ask_llm", return_value=("нативный ответ", {"prompt_tokens": 1, "completion_tokens": 1},
                                                            {"finish_reason": "stop", "tool_calls_total": 0, "error": None,
                                                             "http_latency_ms": 1, "rate_limits": {}, "status": None,
                                                             "retry_after_sec": None})), \
             mock.patch.object(bot, "load_provider_key", return_value="k"), \
             mock.patch.object(bot, "capabilities_for_model", return_value=["text"]), \
             mock.patch.object(bot.DB, "log_request"):
            bot.answer_with_fallback(7, ADMIN, dict(CLAUDE_SESSION), [], "Даров", "sys")
        self.assertEqual(called, [])


_next_update_id = iter(range(70_000, 80_000))


def ask_end_to_end(uid, acpx_result):
    """The whole handler, from the Telegram update down to what is sent back."""
    said = []
    with mock.patch.object(bot, "ensure_access", return_value=True), \
         mock.patch.object(bot, "tg_send_text", side_effect=lambda t, u, text, **k: said.append(text) or {"ok": True}), \
         mock.patch.object(bot, "tg_send_long_text", side_effect=lambda t, u, text, **k: said.append(text) or {"ok": True}), \
         mock.patch.object(bot, "tg_request", return_value={"ok": True}), \
         mock.patch.object(bot, "keep_typing", return_value=lambda: None), \
         mock.patch.object(bot, "live_pairs", return_value=set(LIVE)), \
         mock.patch.object(bot, "ask_via_acpx", return_value=acpx_result), \
         mock.patch.object(bot, "live_model_ranking", return_value=[("openrouter", "good/model")]), \
         mock.patch.object(bot, "ask_llm", return_value=("Привет! Чем могу помочь?", {"prompt_tokens": 1, "completion_tokens": 1},
                                                        {"finish_reason": "stop", "tool_calls_total": 0, "error": None,
                                                         "http_latency_ms": 1, "rate_limits": {}, "status": None,
                                                         "retry_after_sec": None})), \
         mock.patch.object(bot, "load_provider_key", return_value="k"), \
         mock.patch.object(bot, "capabilities_for_model", return_value=["text"]), \
         mock.patch.object(bot, "ensure_text_model_for_session", return_value=("openrouter", "minimax/minimax-m3:free", False)), \
         mock.patch.object(bot.DB, "get_session", return_value=dict(CLAUDE_SESSION)), \
         mock.patch.object(bot.DB, "save_session"), \
         mock.patch.object(bot.DB, "add_usage"), \
         mock.patch.object(bot.DB, "log_request", return_value=1), \
         mock.patch.object(bot.DB, "set_request_delivered"), \
         mock.patch.object(bot.DB, "set_last_session_id"):
        bot.process_update({"update_id": next(_next_update_id),
                            "message": {"message_id": 3, "from": {"id": uid, "username": "u"},
                                        "chat": {"id": uid}, "text": "Даров"}},
                           "token", ADMIN)
    return " ".join(said)


class EndToEnd(unittest.TestCase):
    def test_the_admin_saying_hello_is_answered(self):
        self.assertIn("Привет", ask_end_to_end(ADMIN, sandbox_failed()))

    def test_the_word_model_never_appears_in_the_chat(self):
        self.assertNotIn("model", ask_end_to_end(ADMIN, sandbox_failed()).lower())

    def test_the_words_internal_error_never_appear_in_the_chat(self):
        self.assertNotIn("Internal error", ask_end_to_end(ADMIN, sandbox_failed()))

    def test_a_plain_user_hears_none_of_it_either(self):
        self.assertNotIn("--model", ask_end_to_end(PLAIN_USER, sandbox_failed()))


if __name__ == "__main__":
    unittest.main()
