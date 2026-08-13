"""Scorers for the v2 suite: MMLU-Pro letters and IFEval constraints.

GSM8K was retired because it stopped separating models: 11 of 22 scored exactly
1.00 on their uncut answers. These two scorers must not repeat its mistakes —
no credit without the requested tag, no credit for ignoring a constraint.

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from agent.benchmark_scoring import score, score_ifeval, score_mcq_letter  # noqa: E402

_DATASETS = _BOT_DIR / "benchmark-datasets"


class McqLetter(unittest.TestCase):
    def test_tagged_letter_is_read(self):
        self.assertTrue(score_mcq_letter("Долго думал.\nANSWER: C", "C")[0])

    def test_parenthesised_letter_is_read(self):
        self.assertTrue(score_mcq_letter("ANSWER: (J)", "J")[0])

    def test_last_tag_wins_after_a_correction(self):
        self.assertTrue(score_mcq_letter("ANSWER: A\nНет, пересчитал.\nANSWER: B", "B")[0])

    def test_letter_mentioned_without_the_tag_is_not_an_answer(self):
        ok, sc, detail = score_mcq_letter("Думаю, вариант C выглядит верным", "C")
        self.assertFalse(ok)
        self.assertEqual(detail, "no_letter_in_response")

    def test_wrong_letter_fails(self):
        self.assertFalse(score_mcq_letter("ANSWER: D", "A")[0])


class IfevalConstraints(unittest.TestCase):
    def spec(self, cid, **kw):
        return {"instruction_ids": [cid], "kwargs": [kw]}

    def test_forbidden_word_present_fails(self):
        self.assertFalse(score_ifeval("Yes, it is cold", self.spec("keywords:forbidden_words", forbidden_words=["yes"]))[0])

    def test_forbidden_word_absent_passes(self):
        self.assertTrue(score_ifeval("It is cold", self.spec("keywords:forbidden_words", forbidden_words=["yes"]))[0])

    def test_lowercase_constraint(self):
        spec = self.spec("change_case:english_lowercase")
        self.assertTrue(score_ifeval("all lower case here", spec)[0])
        self.assertFalse(score_ifeval("Not All Lower", spec)[0])

    def test_comma_ban(self):
        spec = self.spec("punctuation:no_comma")
        self.assertTrue(score_ifeval("no commas here", spec)[0])
        self.assertFalse(score_ifeval("one, two", spec)[0])

    def test_partial_credit_when_one_of_two_constraints_holds(self):
        spec = {"instruction_ids": ["punctuation:no_comma", "change_case:english_lowercase"],
                "kwargs": [{}, {}]}
        ok, sc, _ = score_ifeval("lower but, comma", spec)
        self.assertFalse(ok)
        self.assertEqual(sc, 0.5)

    def test_empty_answer_scores_zero(self):
        self.assertEqual(score_ifeval("   ", self.spec("punctuation:no_comma"))[1], 0.0)

    def test_unsupported_constraint_is_refused_not_guessed(self):
        ok, sc, detail = score_ifeval("whatever", self.spec("combination:repeat_prompt"))
        self.assertEqual(sc, 0.0)
        self.assertIn("unsupported_constraint", detail)


class ShippedDatasets(unittest.TestCase):
    """The committed samples must be scoreable — a bad row would silently score zero forever."""

    def test_every_ifeval_row_uses_constraints_we_can_check(self):
        rows = json.loads((_DATASETS / "ifeval.json").read_text())["samples"]
        for row in rows:
            _, _, detail = score_ifeval("some answer", row["ground_truth"])
            self.assertNotIn("unsupported_constraint", detail, row["id"])

    def test_every_mmlu_row_has_a_letter_within_its_options(self):
        rows = json.loads((_DATASETS / "mmlu_pro.json").read_text())["samples"]
        for row in rows:
            truth = row["ground_truth"]
            self.assertIn(truth, "ABCDEFGHIJ", row["id"])
            self.assertIn(f"{truth})", row["question"], row["id"])

    def test_every_math_row_has_an_integer_answer(self):
        rows = json.loads((_DATASETS / "math500_int.json").read_text())["samples"]
        for row in rows:
            self.assertIsInstance(row["ground_truth"], int, row["id"])

    def test_kinds_declared_in_tasks_are_all_implemented(self):
        tasks = json.loads((_BOT_DIR / "benchmark-tasks.json").read_text())
        for group in ("native", "claude"):
            for task in tasks[group]:
                _, _, detail = score(task["kind"], "", None, workspace=None)
                self.assertNotIn("unknown_kind", detail, task["id"])


if __name__ == "__main__":
    unittest.main()
