"""Regression: which model the claude CLI is allowed to be pointed at, and when.

The board ranks models by how well they answer chat completions. claude-code speaks the
Anthropic Messages protocol instead, and the leader of the day was `minimax/minimax-m3:free`,
which it cannot talk to at all. Being live is necessary and not sufficient: only a pair
someone ran through `acpx --model <id> claude exec` goes into the sandbox.

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from agent import model_routing  # noqa: E402

GOOD = ("openrouter", "verified/model:free")
ALSO_GOOD = ("openrouter", "second/model:free")
DEAD = ("openrouter", "minimax/minimax-m3:free")
WHITELIST = (GOOD, ALSO_GOOD)


class WhichModelRunsTheCli(unittest.TestCase):
    def test_a_model_outside_the_list_is_never_chosen(self):
        picked = model_routing.claude_cli_candidate({DEAD, GOOD}, preferred=DEAD, whitelist=WHITELIST)
        self.assertNotEqual(picked, DEAD)

    def test_a_whitelisted_model_takes_its_place(self):
        picked = model_routing.claude_cli_candidate({DEAD, GOOD}, preferred=DEAD, whitelist=WHITELIST)
        self.assertEqual(picked, GOOD)

    def test_a_whitelisted_model_the_probe_cannot_reach_is_skipped(self):
        picked = model_routing.claude_cli_candidate({ALSO_GOOD}, preferred=GOOD, whitelist=WHITELIST)
        self.assertEqual(picked, ALSO_GOOD)

    def test_nothing_live_means_no_candidate(self):
        self.assertIsNone(model_routing.claude_cli_candidate({DEAD}, preferred=DEAD, whitelist=WHITELIST))

    def test_a_verified_pinned_model_is_kept(self):
        picked = model_routing.claude_cli_candidate({GOOD, ALSO_GOOD}, preferred=ALSO_GOOD, whitelist=WHITELIST)
        self.assertEqual(picked, ALSO_GOOD)


class NoOneMillionSuffixInTheList(unittest.TestCase):
    def test_the_shipped_whitelist_carries_no_context_marker(self):
        """`[1m]` is claude-code's own marker for a 1M-context model, not an OpenRouter id."""
        for _, model in model_routing.CLAUDE_CLI_MODELS:
            self.assertNotIn("[1m]", model)


class WhichModeAnswers(unittest.TestCase):
    def test_a_plain_user_is_always_native(self):
        self.assertEqual(model_routing.engine_mode_for("claude", is_admin=False, claude_model=GOOD), "native")

    def test_the_admin_keeps_claude_when_a_model_can_run_it(self):
        self.assertEqual(model_routing.engine_mode_for("claude", is_admin=True, claude_model=GOOD), "claude")

    def test_claude_without_a_runnable_model_falls_back_to_native(self):
        self.assertEqual(model_routing.engine_mode_for("claude", is_admin=True, claude_model=None), "native")

    def test_an_unknown_mode_reads_as_native(self):
        self.assertEqual(model_routing.engine_mode_for("nonsense", is_admin=True), "native")

    def test_an_empty_row_reads_as_native(self):
        self.assertEqual(model_routing.engine_mode_for(None, is_admin=True), "native")

    def test_pi_does_not_need_the_claude_whitelist(self):
        self.assertEqual(model_routing.engine_mode_for("pi", is_admin=True, claude_model=None), "pi")


if __name__ == "__main__":
    unittest.main()
