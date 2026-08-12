"""The scorer must read the answer the task asked for, not guess from the working.

Replaying 2 309 stored responses on 2026-08-12: the "last number in the text"
fallback fired on 30% of them and was right 11.7% of the time — 5 points of
accuracy handed to models that ignored the required format.

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from agent.benchmark_scoring import extract_numeric, score_gsm8k_numeric  # noqa: E402


class TaggedAnswers(unittest.TestCase):
    def test_answer_tag_is_read(self):
        self.assertEqual(extract_numeric("Всего 5 яблок.\nANSWER: 42"), 42.0)

    def test_boxed_is_read(self):
        self.assertEqual(extract_numeric(r"\boxed{18}"), 18.0)

    def test_thousands_separator_survives(self):
        self.assertEqual(extract_numeric("ANSWER: 1,250"), 1250.0)

    def test_last_tag_wins_when_reasoning_restates_it(self):
        self.assertEqual(extract_numeric("ANSWER: 7\n...пересчитал...\nANSWER: 9"), 9.0)


class NoGuessing(unittest.TestCase):
    def test_untagged_working_scores_nothing(self):
        self.assertIsNone(extract_numeric("Сначала 12, потом 30, итого 42"))

    def test_truncated_reasoning_is_not_credited(self):
        cut = "Он купил 3 коробки по 12 штук, это 36, затем добавил 6"
        self.assertIsNone(extract_numeric(cut))

    def test_bare_trailing_number_is_not_an_answer(self):
        self.assertIsNone(extract_numeric("Ответ где-то рядом 42"))


class Scoring(unittest.TestCase):
    def test_correct_tagged_answer_passes(self):
        ok, score, _ = score_gsm8k_numeric("ANSWER: 42", 42)
        self.assertTrue(ok)
        self.assertEqual(score, 1.0)

    def test_untagged_response_is_reported_as_format_miss(self):
        ok, score, detail = score_gsm8k_numeric("итого 42", 42)
        self.assertFalse(ok)
        self.assertEqual(score, 0.0)
        self.assertEqual(detail, "no_number_in_response")

    def test_wrong_tagged_answer_fails(self):
        ok, _, _ = score_gsm8k_numeric("ANSWER: 41", 42)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
