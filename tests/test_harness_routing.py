"""A harness run must go to a provider that can actually answer it.

Measured 2026-08-12 on the live box: the benchmark's claude runs succeed on
openrouter and fail on groq/cerebras/nvidia with "Internal error: There's an
issue with the selected model (…[1m])" — claude-code speaks the Anthropic
Messages protocol at an endpoint that does not implement it. pi speaks native
APIs but only gets a key for openrouter/groq/cerebras; on nvidia it 401s.

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

_spec = importlib.util.spec_from_file_location("bot_harness", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)


def target(agent, provider, model="some/model"):
    with mock.patch.object(bot.DB, "pick_default_text_model", return_value="picked/model"):
        return bot.harness_target(agent, provider, model)


class ClaudeMode(unittest.TestCase):
    def test_openrouter_is_left_alone(self):
        self.assertEqual(target("claude", "openrouter"), ("openrouter", "some/model", False))

    def test_nvidia_is_rerouted_instead_of_failing(self):
        provider, model, switched = target("claude", "nvidia")
        self.assertEqual((provider, switched), ("openrouter", True))
        self.assertEqual(model, "picked/model")

    def test_groq_is_rerouted_too(self):
        self.assertTrue(target("claude", "groq")[2])


class PiMode(unittest.TestCase):
    def test_pi_keeps_groq_because_it_has_that_key(self):
        self.assertFalse(target("pi", "groq")[2])

    def test_pi_keeps_cerebras(self):
        self.assertFalse(target("pi", "cerebras")[2])

    def test_pi_reroutes_nvidia_which_has_no_key_mapping(self):
        self.assertTrue(target("pi", "nvidia")[2])


class UnknownAgent(unittest.TestCase):
    def test_unknown_mode_defaults_to_the_strict_list(self):
        self.assertTrue(target("whatever", "groq")[2])

    def test_mode_name_maps_to_a_known_agent(self):
        self.assertEqual(bot.acp_agent_for_mode("pi"), "pi")
        self.assertEqual(bot.acp_agent_for_mode("nonsense"), "claude")


if __name__ == "__main__":
    unittest.main()
