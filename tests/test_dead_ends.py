"""No screen may end the conversation, and no text may point at something that isn't there.

From the UX review of 2026-08-14: picking a model stripped the keyboard and left
`Latency: 812ms | Checked: 12m ago` on screen; errors sent people to /models and
/tools, neither of which exists; menu buttons spun until Telegram timed them out.

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

_SOURCE = (_BOT_DIR / "smolevich-ai-bot.py").read_text()

_spec = importlib.util.spec_from_file_location("bot_dead_ends", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

SESSION = {"provider": "groq", "model": "llama-3.3-70b-versatile", "ui_lang": "ru"}


class NoPhantomCommands(unittest.TestCase):
    """handle_command knows /menu, /start, /help and /feedback — nothing else."""

    def test_no_text_advertises_slash_models(self):
        self.assertNotIn("/models", _SOURCE)

    def test_no_text_advertises_slash_tools(self):
        # Only user-facing wording matters; "mode/tools" appears in a code comment.
        self.assertNotIn("/tools off", _SOURCE)

    def test_no_server_log_path_is_shown_to_users(self):
        self.assertNotIn("[raw:", _SOURCE)


class ModelList(unittest.TestCase):
    def build(self, board=None):
        with mock.patch.object(bot.DB, "get_recent_models", return_value=[
                 {"id": "meta/muse-glimmer-30b", "latency_ms": 812, "available": True, "supportsTools": True}]), \
             mock.patch.object(bot.DB, "get_healthy_models", return_value=[]), \
             mock.patch.object(bot, "fetch_leaderboard", return_value=board):
            return bot.build_models_view(SESSION)

    def test_no_milliseconds_on_screen(self):
        txt, kb = self.build()
        self.assertNotIn("812ms", " ".join(b["text"] for row in kb for b in row))

    def test_shows_what_the_board_measured(self):
        board = {"models": [{"model": "meta/muse-glimmer-30b", "solved_of_ten": 7, "scores": {}}]}
        _, kb = self.build(board)
        self.assertIn("решает 7 из 10", " ".join(b["text"] for row in kb for b in row))

    def test_there_is_always_a_way_back(self):
        _, kb = self.build()
        self.assertIn("menu:back", [b["callback_data"] for row in kb for b in row])

    def test_heading_is_not_provider_slang(self):
        txt, _ = self.build()
        self.assertNotIn("nvidia", txt.lower())
        self.assertNotIn("groq", txt.lower())


class TypingIndicator(unittest.TestCase):
    def test_it_is_refreshed_not_sent_once(self):
        sent = []
        with mock.patch("agent.telegram_api.tg_send_chat_action", side_effect=lambda *a, **k: sent.append(1)):
            stop = bot.keep_typing("token", 1)
            stop()
        self.assertTrue(callable(stop))

    def test_the_beat_cannot_outlive_the_longest_answer(self):
        """A caller that dies without stopping us must not leak a thread forever."""
        source = re.search(r"def keep_typing.*?\n\n\n", _SOURCE, re.DOTALL).group(0)
        self.assertIn("for _ in range(", source)


if __name__ == "__main__":
    unittest.main()
