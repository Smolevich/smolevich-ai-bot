"""Замер не может просить больше, чем провайдер согласен отдать за один запрос.

04–08.09.2026 `groq/qwen/qwen3.8-27b` 96 раз вернул «on output tokens per minute
(OTPM): Limit 1000, Requested 1024». Набор задач просит 1024, 1536 и 2048 — такой
запрос не пройдёт никогда, сколько его ни повторяй, и модель выпадала из замера.

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from agent.rate_limits import (  # noqa: E402
    CEILING_MARGIN,
    capped_max_tokens,
    output_ceiling,
    output_ceiling_from_error,
)

_spec = importlib.util.spec_from_file_location("model_benchmark_ceiling", _BOT_DIR / "model-benchmark.py")
assert _spec and _spec.loader
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)

CAPPED = ("groq", "qwen/qwen3.8-27b")
SAMPLE = {"question": "2+2?"}
REAL_429 = ('HTTP 429: Too Many Requests {"error":{"message":"Request too large for model '
            '`qwen/qwen3.8-27b` in organization `org_x` service tier `on_demand` on output '
            'tokens per minute (OTPM): Limit 1000, Requested 1024."}}')


class TheCap(unittest.TestCase):
    def test_max_tokens_never_exceeds_the_known_ceiling(self):
        self.assertLess(capped_max_tokens(*CAPPED, 2048), output_ceiling(*CAPPED))

    def test_the_cap_keeps_a_margin_under_the_ceiling(self):
        ceiling = output_ceiling(*CAPPED)
        self.assertLessEqual(capped_max_tokens(*CAPPED, 2048), ceiling * (1 - CEILING_MARGIN))

    def test_a_smaller_request_is_left_alone(self):
        self.assertEqual(capped_max_tokens(*CAPPED, 256), 256)

    def test_a_model_with_no_known_ceiling_is_not_touched(self):
        self.assertEqual(capped_max_tokens("nvidia", "meta/llama-3.2-11b", 2048), 2048)

    def test_the_cap_never_returns_zero(self):
        self.assertGreaterEqual(capped_max_tokens(*CAPPED, 1), 1)


class ReadingTheCeilingBackFromA429(unittest.TestCase):
    def test_the_limit_is_taken_from_the_real_error_body(self):
        self.assertEqual(output_ceiling_from_error(REAL_429), 1000)

    def test_an_ordinary_rate_limit_carries_no_ceiling(self):
        self.assertIsNone(output_ceiling_from_error("HTTP 429: Rate limit reached ... (TPD): Limit 200000"))

    def test_no_error_text_is_not_a_crash(self):
        self.assertIsNone(output_ceiling_from_error(None))


class WhatTheBenchmarkActuallyAsksFor(unittest.TestCase):
    def payload(self, provider, model_id, want):
        return mb.native_payload(provider, model_id, {"max_tokens": want}, SAMPLE)

    def test_the_request_carries_the_capped_number(self):
        self.assertEqual(self.payload(*CAPPED, 1024)["max_tokens"], capped_max_tokens(*CAPPED, 1024))

    def test_the_request_for_a_capped_model_stays_under_its_ceiling(self):
        self.assertLess(self.payload(*CAPPED, 2048)["max_tokens"], output_ceiling(*CAPPED))

    def test_an_uncapped_model_still_gets_the_full_budget(self):
        self.assertEqual(self.payload("nvidia", "meta/llama-3.2-11b", 2048)["max_tokens"], 2048)

    def test_every_task_in_the_shipped_suite_fits_under_the_cap(self):
        """Регресс: замер просил 1024 при потолке 1000 и падал на каждом сэмпле."""
        suite = json.loads((_BOT_DIR / "benchmark-tasks.json").read_text())
        for task in suite["native"] + suite["claude"]:
            with self.subTest(task=task["id"]):
                asked = self.payload(*CAPPED, int(task["max_tokens"]))["max_tokens"]
                self.assertLess(asked, output_ceiling(*CAPPED))


if __name__ == "__main__":
    unittest.main()
