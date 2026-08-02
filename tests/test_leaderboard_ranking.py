"""Ranking rules for the published free-models leaderboard.

These lock in the fixes for a board that put a model with 2 finished runs above
one with 8, tagged a 4.8-second model "Fast", and reshuffled between runs on
single flipped answers.

Stdlib only, like the rest of the project. `model-benchmark.py` is hyphenated
and cannot be imported normally, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
# The module does `from agent.acpx_lock import ...`, so bot/ must be importable.
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

_MODULE_PATH = _BOT_DIR / "model-benchmark.py"
_spec = importlib.util.spec_from_file_location("model_benchmark", _MODULE_PATH)
assert _spec and _spec.loader
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)


def entry(name, *, score, runs, last_bench=0, provisional=None):
    """A leaderboard row as `leaderboard_payload` builds it."""
    return {
        "name": name,
        "score": score,
        "runs": runs,
        "last_bench": last_bench,
        "provisional": mb.is_provisional(runs) if provisional is None else provisional,
    }


def order(rows):
    return [r["name"] for r in sorted(rows, key=mb.leaderboard_sort_key, reverse=True)]


class ComputeOverall(unittest.TestCase):
    def test_availability_outweighs_bench_score(self):
        """Uptime has millions of probes behind it, bench score a handful."""
        reliable_mediocre = mb.compute_overall(health_rate=1.0, bench_score=0.0)
        flaky_brilliant = mb.compute_overall(health_rate=0.0, bench_score=1.0)
        self.assertGreater(reliable_mediocre, flaky_brilliant)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(mb.compute_overall(1.0, 1.0), 1.0)
        self.assertAlmostEqual(mb.compute_overall(0.0, 0.0), 0.0)

    def test_latency_is_not_an_input(self):
        """Regression: `latency_bonus` used to be added straight into overall."""
        self.assertNotIn("latency", mb.compute_overall.__code__.co_varnames)


class ProvisionalThreshold(unittest.TestCase):
    def test_below_threshold_is_provisional(self):
        self.assertTrue(mb.is_provisional(0))
        self.assertTrue(mb.is_provisional(mb.MIN_RUNS_TO_RANK - 1))

    def test_at_threshold_is_ranked(self):
        self.assertFalse(mb.is_provisional(mb.MIN_RUNS_TO_RANK))
        self.assertFalse(mb.is_provisional(mb.MIN_RUNS_TO_RANK + 10))


class SortOrder(unittest.TestCase):
    def test_two_run_model_cannot_outrank_eight_run_model(self):
        """The exact failure seen on the published board."""
        rows = [
            entry("llama-3.2-11b-vision", score=0.762, runs=2),
            entry("gpt-oss-20b", score=0.728, runs=8),
            entry("qwen-3-32b", score=0.722, runs=6),
        ]
        self.assertEqual(order(rows), ["gpt-oss-20b", "qwen-3-32b", "llama-3.2-11b-vision"])

    def test_provisional_sinks_even_with_a_perfect_score(self):
        rows = [
            entry("unmeasured", score=1.0, runs=1),
            entry("measured", score=0.30, runs=20),
        ]
        self.assertEqual(order(rows), ["measured", "unmeasured"])

    def test_run_count_breaks_ties_within_hysteresis_band(self):
        """Scores differing by thousandths must not swap positions."""
        rows = [
            entry("fewer_runs", score=0.7009, runs=5),
            entry("more_runs", score=0.7001, runs=40),
        ]
        self.assertEqual(order(rows), ["more_runs", "fewer_runs"])

    def test_real_gap_still_decides(self):
        """Hysteresis must not flatten genuine differences."""
        rows = [
            entry("clearly_better", score=0.80, runs=5),
            entry("clearly_worse", score=0.60, runs=40),
        ]
        self.assertEqual(order(rows), ["clearly_better", "clearly_worse"])

    def test_latency_is_absent_from_the_sort_key(self):
        slow = entry("slow", score=0.70, runs=10)
        fast = entry("fast", score=0.70, runs=10)
        slow["latency_ms"], fast["latency_ms"] = 9000, 100
        self.assertEqual(mb.leaderboard_sort_key(slow), mb.leaderboard_sort_key(fast))

    def test_a_slower_model_still_wins_on_a_better_score(self):
        """Regression: latency_bonus could lift a fast model over a better one."""
        slow_but_good = entry("slow_but_good", score=0.80, runs=10)
        fast_but_worse = entry("fast_but_worse", score=0.60, runs=10)
        slow_but_good["latency_ms"], fast_but_worse["latency_ms"] = 9000, 100
        self.assertEqual(
            order([fast_but_worse, slow_but_good]),
            ["slow_but_good", "fast_but_worse"],
        )

    def test_sort_key_tolerates_missing_fields(self):
        self.assertIsInstance(mb.leaderboard_sort_key({}), tuple)


class Status(unittest.TestCase):
    def test_unavailable_beats_every_other_label(self):
        self.assertEqual(mb.rank_status(0.5, 0.9, provisional=False), "unstable")
        self.assertEqual(mb.rank_status(0.5, 0.9, provisional=True), "unstable")

    def test_provisional_when_healthy_but_unmeasured(self):
        self.assertEqual(mb.rank_status(0.99, 0.9, provisional=True), "provisional")

    def test_available_when_healthy_and_measured(self):
        self.assertEqual(mb.rank_status(0.99, 0.9, provisional=False), "available")

    def test_weak_bench_marks_unstable(self):
        self.assertEqual(mb.rank_status(0.99, 0.2, provisional=False), "unstable")


class Strengths(unittest.TestCase):
    def test_quality_tags_withheld_until_enough_runs(self):
        tags = mb.infer_strengths("some/model", 0.95, 0.95, 900, total_runs=2)
        self.assertNotIn("Stable chat", tags)
        self.assertNotIn("Agent mode", tags)

    def test_quality_tags_granted_once_measured(self):
        tags = mb.infer_strengths("some/model", 0.95, 0.95, 900, total_runs=10)
        self.assertIn("Stable chat", tags)
        self.assertIn("Agent mode", tags)

    def test_fast_tag_respects_the_threshold(self):
        quick = mb.infer_strengths("m", 0.9, 0.0, mb.FAST_LATENCY_MS - 100, total_runs=10)
        sluggish = mb.infer_strengths("m", 0.9, 0.0, 4800, total_runs=10)
        self.assertIn("Fast", quick)
        self.assertNotIn("Fast", sluggish)

    def test_no_unconditional_language_tag(self):
        """It used to be appended to every model, so it carried no information."""
        tags = mb.infer_strengths("some/model", 0.1, 0.1, 9000, total_runs=10)
        self.assertNotIn("Russian/English", tags)

    def test_tag_list_stays_short(self):
        tags = mb.infer_strengths("qwen-coder-70b-reason", 0.99, 0.99, 100, total_runs=99)
        self.assertLessEqual(len(tags), 4)


if __name__ == "__main__":
    unittest.main()
