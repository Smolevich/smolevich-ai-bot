"""«🤖 Выбрать вручную» показывал 12 имён без единого признака — выбирать было не по чему.

2026-09-07: за «Для любопытных» пряталась вторая, более длинная витрина тех же моделей,
только без порядка и без пометок. Человеку остаётся пятёрка с признаками, которые
действительно есть в замере; всё остальное — админу.

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

from agent.model_routing import badge_keys  # noqa: E402

_spec = importlib.util.spec_from_file_location("bot_manual_choice", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

ADMIN = 1
USER = 7
SESSION = {"provider": "groq", "model": "llama-3.1-8b-instant", "ui_lang": "ru",
           "tools_enabled": True, "history": [], "engine_mode": "native"}

RANKED = [("groq", "llama-3.1-8b-instant"),
          ("openrouter", "cohere/north-mini-code:free"),
          ("nvidia", "meta/llama-3.2-11b")]
LATENCY = {("groq", "llama-3.1-8b-instant"): 900,
           ("openrouter", "cohere/north-mini-code:free"): 120,
           ("nvidia", "meta/llama-3.2-11b"): 4000}


def curious(is_admin=False, ranked=None, latency=None):
    with mock.patch.object(bot, "live_model_ranking", return_value=list(RANKED if ranked is None else ranked)), \
         mock.patch.object(bot, "live_latency_map", return_value=dict(LATENCY if latency is None else latency)):
        return bot.build_curious_view(SESSION, is_admin=is_admin)


def press(data, uid):
    drawn = []

    def fake_request(_t, method, payload=None):
        if method in ("editMessageText", "sendMessage"):
            drawn.append(payload)
        return {"ok": True}

    cb = {"id": "1", "from": {"id": uid}, "data": data,
          "message": {"chat": {"id": uid}, "message_id": 42}}
    with mock.patch.object(bot, "tg_request", side_effect=fake_request), \
         mock.patch.object(bot.DB, "get_session", return_value=dict(SESSION)), \
         mock.patch.object(bot.DB, "log_ui_event"), \
         mock.patch.object(bot.DB, "get_recent_models", return_value=[
             {"id": "a/b", "latency_ms": 10, "available": True, "supportsTools": True}]), \
         mock.patch.object(bot.DB, "get_healthy_models", return_value=[]), \
         mock.patch.object(bot, "live_model_ranking", return_value=list(RANKED)), \
         mock.patch.object(bot, "live_latency_map", return_value=dict(LATENCY)):
        bot.handle_callback(cb, "token", admin_id=ADMIN)
    return drawn


def routes(kb):
    return [b["callback_data"] for row in kb for b in row]


class BadgesComeFromTheMeasurement(unittest.TestCase):
    def test_the_first_place_is_the_steadiest(self):
        self.assertEqual(badge_keys(RANKED, LATENCY)[RANKED[0]], "steadiest")

    def test_the_lowest_measured_latency_is_the_fastest(self):
        self.assertEqual(badge_keys(RANKED, LATENCY)[RANKED[1]], "fastest")

    def test_a_model_the_probe_never_timed_gets_no_speed_badge(self):
        badges = badge_keys(RANKED, {})
        self.assertNotIn("fastest", badges.values())

    def test_one_model_never_carries_two_badges(self):
        badges = badge_keys(RANKED, {RANKED[0]: 5})
        self.assertEqual(badges[RANKED[0]], "steadiest")

    def test_an_empty_ranking_has_no_badges(self):
        self.assertEqual(badge_keys([], LATENCY), {})


class WhatANonAdminSees(unittest.TestCase):
    def test_no_more_than_five_names(self):
        many = [("groq", f"m-{i}") for i in range(20)]
        txt, _ = curious(ranked=many, latency={})
        self.assertLessEqual(len([l for l in txt.splitlines() if l.startswith("• ")]), 5)

    def test_the_steadiest_is_named_as_such(self):
        txt, _ = curious()
        self.assertIn("✔ самая стабильная", txt)

    def test_the_fastest_is_named_as_such(self):
        txt, _ = curious()
        self.assertIn("⚡ быстрая", txt)

    def test_nothing_claims_long_texts(self):
        """contextWindow борда пуст у всех строк (проверено 08.09.2026) — признака нет."""
        txt, _ = curious()
        self.assertNotIn("длинные тексты", txt)

    def test_the_second_list_of_the_same_models_is_gone(self):
        _, kb = curious()
        self.assertNotIn("menu:model", routes(kb))

    def test_a_stale_manual_button_still_lands_on_a_screen(self):
        drawn = press("menu:model", USER)
        self.assertTrue(drawn)

    def test_a_stale_manual_button_does_not_open_a_model_list(self):
        drawn = press("menu:model", USER)
        self.assertNotIn("set_model:a/b", routes(drawn[0]["reply_markup"]["inline_keyboard"]))


class WhatTheAdminKeeps(unittest.TestCase):
    def test_the_admin_still_has_the_full_list(self):
        _, kb = curious(is_admin=True)
        self.assertIn("menu:model", routes(kb))

    def test_the_admin_list_still_opens(self):
        drawn = press("menu:model", ADMIN)
        self.assertIn("set_model:a/b", routes(drawn[0]["reply_markup"]["inline_keyboard"]))


if __name__ == "__main__":
    unittest.main()
