"""The board must compare like with like.

Fixed here, all seen on the live board:
- the top-1 model of each provider was scored by a different formula than the rest,
  so the model chosen for the deeper check was punished for having been checked;
- latency averaged a one-token health ping with a full generation, and the "Fast"
  tag hung off that average;
- "Code" and "Reasoning" were guessed from substrings in the model id;
- the board was cut to one model per provider, discarding three quarters of the runs;
- agent-mode jobs were queued for providers that cannot answer the protocol.

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

_spec = importlib.util.spec_from_file_location("model_benchmark_fair", _BOT_DIR / "model-benchmark.py")
assert _spec and _spec.loader
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)


class SampleThreshold(unittest.TestCase):
    def test_threshold_counts_samples_not_runs(self):
        """One run writes samples_per_run rows, so a threshold of 4 was cleared instantly."""
        self.assertGreaterEqual(mb.MIN_SAMPLES_TO_RANK, 20)

    def test_a_single_run_worth_of_samples_is_still_provisional(self):
        self.assertTrue(mb.is_provisional(5))

    def test_four_runs_worth_is_ranked(self):
        self.assertFalse(mb.is_provisional(20))


class Tags(unittest.TestCase):
    def test_code_tag_is_not_guessed_from_the_name(self):
        self.assertNotIn("Code", mb.infer_strengths("deepseek/deepseek-coder", 0.9, 0.0, 900, 40))

    def test_reasoning_tag_is_not_guessed_from_the_name(self):
        self.assertNotIn("Reasoning", mb.infer_strengths("meta/llama-70b", 0.9, 0.0, 900, 40))

    def test_measured_tags_survive(self):
        tags = mb.infer_strengths("any/model", 0.95, 0.95, 900, 40)
        self.assertIn("Stable chat", tags)
        self.assertIn("Agent mode", tags)


class HarnessProviders(unittest.TestCase):
    def test_agent_mode_is_limited_to_providers_that_answer_it(self):
        self.assertEqual(tuple(mb.HARNESS_PROVIDERS), ("openrouter",))

    def test_the_providers_that_returned_1m_errors_are_excluded(self):
        for provider in ("groq", "cerebras", "nvidia"):
            self.assertNotIn(provider, mb.HARNESS_PROVIDERS)


class Throttling(unittest.TestCase):
    """Concurrency alone did not stop 429s: the free tiers meter per minute."""

    def setUp(self):
        sys.path.insert(0, str(_BOT_DIR))
        from temporal_jobs import shared  # noqa: PLC0415 — optional dependency-free module
        self.shared = shared

    def test_metered_providers_are_paced_for_their_ceiling(self):
        """groq: 6000 tokens/min ÷ ~1500 an answer ≈ 4/min. cerebras: 4 requests/min."""
        for provider in ("groq", "cerebras"):
            self.assertGreaterEqual(self.shared.PROVIDER_PAUSE_SEC[provider], 12.0, provider)
            self.assertEqual(self.shared.PROVIDER_CONCURRENCY[provider], 1, provider)

    def test_unmetered_providers_are_not_slowed_down(self):
        """nvidia and openrouter returned zero 429s across 480 samples."""
        for provider in ("nvidia", "openrouter"):
            self.assertGreaterEqual(self.shared.PROVIDER_CONCURRENCY[provider], 3, provider)

    def test_every_provider_has_a_pause(self):
        for provider in self.shared.PROVIDER_CONCURRENCY:
            self.assertIn(provider, self.shared.PROVIDER_PAUSE_SEC, provider)

    def test_groq_stays_the_most_restricted(self):
        pauses = self.shared.PROVIDER_PAUSE_SEC
        self.assertGreaterEqual(pauses["groq"], pauses["openrouter"])


if __name__ == "__main__":
    unittest.main()
