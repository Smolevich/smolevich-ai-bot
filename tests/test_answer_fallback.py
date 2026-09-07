"""Regression: a dead model is replaced by the bot, not by the person.

2026-09-07, the whole exchange this locks in:
  Стас: "LinkedIn, Threads что такое…" → "Не получилось получить ответ. Попробуй другую модель."
  Стас: (повторил)                     → "У этой модели кончился бесплатный лимит (None)."
Two live models were sitting right there and neither was tried.

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

_spec = importlib.util.spec_from_file_location("bot_fallback", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

DEAD = ("nvidia", "nvidia/nemotron-3-nano-30b-a3b")
BUSY = ("groq", "llama-3.3-70b-versatile")
GOOD = ("openrouter", "minimax/minimax-m3:free")
RANKED = [DEAD, BUSY, GOOD]

SESSION = {"provider": DEAD[0], "model": DEAD[1], "ui_lang": "ru", "tools_enabled": False,
           "engine_mode": "native", "history": [], "model_pinned": False}


def run(session=None, answers=None, ranked=None):
    """Drive answer_with_fallback with a scripted reply per (provider, model)."""
    tried = []

    def fake_ask(api_url, api_key, model, messages, **kw):
        tried.append(model)
        reply = (answers or {}).get(model, ("HTTP 410", 410))
        if isinstance(reply, tuple):
            error, status = reply
            return None, {"prompt_tokens": 0, "completion_tokens": 0}, {
                "finish_reason": None, "tool_calls_total": 0, "error": error,
                "http_latency_ms": 1, "rate_limits": {}, "status": status, "retry_after_sec": None}
        return reply, {"prompt_tokens": 1, "completion_tokens": 1}, {
            "finish_reason": "stop", "tool_calls_total": 0, "error": None,
            "http_latency_ms": 1, "rate_limits": {}, "status": None, "retry_after_sec": None}

    with mock.patch.object(bot, "live_model_ranking", return_value=list(RANKED if ranked is None else ranked)), \
         mock.patch.object(bot, "ask_llm", side_effect=fake_ask), \
         mock.patch.object(bot, "load_provider_key", return_value="k"), \
         mock.patch.object(bot, "capabilities_for_model", return_value=["text"]), \
         mock.patch.object(bot.DB, "log_request"):
        result = bot.answer_with_fallback(1, 0, dict(SESSION if session is None else session), [], "?", "sys")
    return result, tried


class DeadDefaultIsReplacedSilently(unittest.TestCase):
    def test_a_gone_model_does_not_end_the_conversation(self):
        (answer, _usage, _meta, _p, _m), _tried = run(answers={GOOD[1]: "LinkedIn — соцсеть."})
        self.assertEqual(answer, "LinkedIn — соцсеть.")

    def test_the_next_model_is_tried_without_asking_the_person(self):
        _, tried = run(answers={GOOD[1]: "ok"})
        self.assertEqual(tried, [DEAD[1], BUSY[1], GOOD[1]])

    def test_a_rate_limited_model_is_not_the_end_of_the_chain(self):
        _, tried = run(answers={BUSY[1]: ("HTTP 429", 429), GOOD[1]: "ok"},
                       ranked=[BUSY, GOOD])
        self.assertEqual(tried, [BUSY[1], GOOD[1]])

    def test_the_answering_model_is_reported_back_to_the_caller(self):
        (_a, _u, _m, provider, model), _ = run(answers={GOOD[1]: "ok"})
        self.assertEqual((provider, model), GOOD)

    def test_only_when_everything_refuses_is_there_no_answer(self):
        (answer, _u, _m, _p, _mo), tried = run(answers={})
        self.assertIsNone(answer)
        self.assertEqual(len(tried), 3)

    def test_every_failed_attempt_becomes_a_database_row(self):
        """The board and the digest see nothing that is not in request_log."""
        with mock.patch.object(bot, "live_model_ranking", return_value=list(RANKED)), \
             mock.patch.object(bot, "ask_llm", return_value=(None, {"prompt_tokens": 0, "completion_tokens": 0},
                                                             {"error": "HTTP 410", "status": 410,
                                                              "finish_reason": None, "tool_calls_total": 0,
                                                              "http_latency_ms": 1})), \
             mock.patch.object(bot, "load_provider_key", return_value="k"), \
             mock.patch.object(bot, "capabilities_for_model", return_value=["text"]), \
             mock.patch.object(bot.DB, "log_request") as logged:
            bot.answer_with_fallback(1, 0, dict(SESSION), [], "?", "sys")
        self.assertEqual(logged.call_count, 3)


class AChosenModelIsKept(unittest.TestCase):
    def test_a_pinned_model_is_tried_first(self):
        session = dict(SESSION, provider=GOOD[0], model=GOOD[1], model_pinned=True)
        _, tried = run(session=session, answers={GOOD[1]: "ok"})
        self.assertEqual(tried[0], GOOD[1])

    def test_an_unpinned_session_starts_from_the_leader_not_from_its_old_row(self):
        session = dict(SESSION, provider=GOOD[0], model=GOOD[1], model_pinned=False)
        _, tried = run(session=session, answers={DEAD[1]: "ok"})
        self.assertEqual(tried[0], DEAD[1])

    def test_a_pinned_model_that_died_is_skipped_rather_than_retried_forever(self):
        session = dict(SESSION, provider="nvidia", model="deleted-yesterday", model_pinned=True)
        _, tried = run(session=session, answers={DEAD[1]: "ok"})
        self.assertNotIn("deleted-yesterday", tried)


if __name__ == "__main__":
    unittest.main()
