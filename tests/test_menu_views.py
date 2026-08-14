"""Menu rules: never offer a model the probes call dead, and let everyone see the top.

Stdlib only, like the rest of the project. `smolevich-ai-bot.py` is hyphenated
and cannot be imported normally, so it is loaded by path.
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

_spec = importlib.util.spec_from_file_location("bot_main", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

SESSION = {"provider": "groq", "model": "llama-3.3-70b-versatile", "ui_lang": "ru"}


def model_row(mid, available):
    return {"id": mid, "latency_ms": 100, "available": available, "supportsTools": True}


def labels(kb):
    return [btn["callback_data"] for row in kb for btn in row]


class ModelsView(unittest.TestCase):
    def build(self, recent, healthy=()):
        with mock.patch.object(bot.DB, "get_recent_models", return_value=list(recent)), \
             mock.patch.object(bot.DB, "get_healthy_models", return_value=list(healthy)), \
             mock.patch.object(bot.DB, "get_model_info", return_value=None):
            return bot.build_models_view(SESSION)

    def test_dead_model_is_not_offered(self):
        _, kb = self.build([model_row("alive", True), model_row("dead", False)])
        self.assertNotIn("set_model:dead", labels(kb))

    def test_live_model_is_offered(self):
        _, kb = self.build([model_row("alive", True), model_row("dead", False)])
        self.assertIn("set_model:alive", labels(kb))

    def test_falls_back_to_healthy_when_nothing_is_fresh(self):
        _, kb = self.build([model_row("dead", False)], healthy=[{"id": "spare", "latency_ms": 50, "supportsTools": False}])
        self.assertIn("set_model:spare", labels(kb))


class MenuRoot(unittest.TestCase):
    def build(self, is_admin):
        with mock.patch.object(bot, "has_stt_models", return_value=False), \
             mock.patch.object(bot, "has_tts_models", return_value=False), \
             mock.patch.object(bot, "has_video_detector", return_value=False):
            return bot.build_menu_root(SESSION, is_admin=is_admin)

    def test_board_is_not_buried_in_a_submenu(self):
        """It moved to the bottom keyboard: the product must open in one tap, not three."""
        _, kb = self.build(is_admin=False)
        self.assertNotIn("menu:top", labels(kb))
        with mock.patch.object(bot, "has_stt_models", return_value=False), \
             mock.patch.object(bot, "has_tts_models", return_value=False):
            bottom = bot.build_quick_keyboard(SESSION)
        self.assertIn(bot.QUICK_BOARD["ru"], [b["text"] for row in bottom["keyboard"] for b in row])

    def test_admin_menu_does_not_duplicate_the_board(self):
        self.assertNotIn("menu:top", labels(bot.build_admin_menu(SESSION)[1]))


if __name__ == "__main__":
    unittest.main()
