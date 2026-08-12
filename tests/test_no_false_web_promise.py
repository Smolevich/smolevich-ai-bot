"""A plain user's sandbox runs with --network=none, so nothing may promise them the web.

The bot used to answer "sure, I can search" to everyone: the system prompt told it
to use DuckDuckGo regardless of role, and the execute_bash schema said "Admin has
internet" without saying what a non-admin gets.

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

_spec = importlib.util.spec_from_file_location("bot_prompt", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)


def bash_description(tools):
    for t in tools:
        if t["function"]["name"] == "execute_bash":
            return t["function"]["description"]
    raise AssertionError("execute_bash is missing")


class SystemPrompt(unittest.TestCase):
    def test_user_prompt_does_not_mention_search_engines(self):
        prompt = bot.build_system_prompt(is_admin=False).lower()
        self.assertNotIn("duckduckgo", prompt)
        self.assertNotIn("google", prompt)

    def test_user_prompt_states_there_is_no_network(self):
        self.assertIn("networking disabled", bot.build_system_prompt(is_admin=False))

    def test_admin_keeps_the_web(self):
        self.assertIn("duckduckgo", bot.build_system_prompt(is_admin=True).lower())


class ToolSchema(unittest.TestCase):
    def test_user_schema_says_no_network(self):
        self.assertIn("NO NETWORK", bash_description(bot.tools_for(is_admin=False)))

    def test_admin_schema_keeps_network(self):
        self.assertIn("network access", bash_description(bot.tools_for(is_admin=True)))

    def test_building_user_tools_does_not_mutate_the_shared_list(self):
        bot.tools_for(is_admin=False)
        self.assertIn("network access", bash_description(bot.TOOLS))


if __name__ == "__main__":
    unittest.main()
