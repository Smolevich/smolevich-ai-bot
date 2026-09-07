"""Regression: the bot picks the model, and picks another one when the first fails.

2026-09-07: the default model was `nvidia/nemotron-3-nano-30b-a3b`, which NVIDIA had
delisted six days earlier (HTTP 410). The bot answered "Не получилось получить ответ.
Попробуй другую модель.", then "У этой модели кончился бесплатный лимит (None)", and
made the person go and choose one from a list.

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from agent import model_routing as mr  # noqa: E402

NOW = 1_757_000_000


def health(provider, model, available=True, age=0):
    return {"provider": provider, "model_id": model, "available": available, "last_check": NOW - age}


BOARD = [
    {"provider": "openrouter", "model": "minimax/minimax-m3:free", "solved_of_ten": 8},
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "solved_of_ten": 6},
    {"provider": "nvidia", "model": "nvidia/nemotron-3-nano-30b-a3b", "solved_of_ten": 5},
]
ROWS = [
    health("openrouter", "minimax/minimax-m3:free"),
    health("groq", "llama-3.3-70b-versatile"),
    health("nvidia", "nvidia/nemotron-3-nano-30b-a3b"),
]


def rank(rows=None, board=None, stats=None, parked=frozenset()):
    rows = ROWS if rows is None else rows
    live = mr.live_candidates(rows, NOW, parked=parked)
    return mr.rank_candidates(BOARD if board is None else board, live, stats or {})


class DefaultIsTheMeasuredLeader(unittest.TestCase):
    def test_default_is_the_leader_of_the_measurement(self):
        self.assertEqual(mr.pick_default(rank()), ("openrouter", "minimax/minimax-m3:free"))

    def test_default_is_not_a_hardcoded_constant(self):
        """Flip the scores and the default follows — it is read, not written into the code."""
        board = [dict(e, solved_of_ten=10 - e["solved_of_ten"]) for e in BOARD]
        self.assertEqual(mr.pick_default(rank(board=board)), ("nvidia", "nvidia/nemotron-3-nano-30b-a3b"))

    def test_success_rate_breaks_a_tie_between_equal_scores(self):
        board = [dict(e, solved_of_ten=7) for e in BOARD]
        stats = {("openrouter", "minimax/minimax-m3:free"): (1, 10),
                 ("groq", "llama-3.3-70b-versatile"): (9, 10)}
        self.assertEqual(mr.pick_default(rank(board=board, stats=stats)),
                         ("groq", "llama-3.3-70b-versatile"))


class ParkingMovesTheDefault(unittest.TestCase):
    def test_parking_the_leader_moves_the_default_to_the_next_one(self):
        ranked = rank(parked={"openrouter"})
        self.assertEqual(mr.pick_default(ranked), ("groq", "llama-3.3-70b-versatile"))

    def test_a_parked_provider_is_not_in_the_chain_at_all(self):
        chain = mr.fallback_chain(rank(parked={"openrouter"}), None)
        self.assertNotIn("openrouter", [provider for provider, _ in chain])

    def test_a_provider_parked_until_a_human_returns_it_counts_as_parked(self):
        state = [{"provider": "nvidia", "disabled_until": -1}]
        self.assertEqual(mr.parked_providers(state, NOW), {"nvidia"})

    def test_an_expired_parking_is_not_parking(self):
        state = [{"provider": "nvidia", "disabled_until": NOW - 10}]
        self.assertEqual(mr.parked_providers(state, NOW), set())


class ProbeFailureRemovesTheModel(unittest.TestCase):
    def test_a_model_that_failed_the_probe_disappears_from_the_list(self):
        rows = [health("openrouter", "minimax/minimax-m3:free", available=False)] + ROWS[1:]
        self.assertNotIn(("openrouter", "minimax/minimax-m3:free"), rank(rows=rows))

    def test_a_delisted_model_the_probe_stopped_touching_disappears(self):
        """nemotron-3-nano: NVIDIA dropped it from /v1/models, so its row simply froze."""
        rows = ROWS[:2] + [health("nvidia", "nvidia/nemotron-3-nano-30b-a3b", age=7 * 24 * 3600)]
        self.assertNotIn(("nvidia", "nvidia/nemotron-3-nano-30b-a3b"), rank(rows=rows))

    def test_a_model_that_passes_the_next_probe_comes_back(self):
        rows = [health("openrouter", "minimax/minimax-m3:free", available=False)] + ROWS[1:]
        self.assertNotIn(("openrouter", "minimax/minimax-m3:free"), rank(rows=rows))
        self.assertIn(("openrouter", "minimax/minimax-m3:free"), rank(rows=ROWS))

    def test_zero_of_ten_is_a_failed_measurement_not_a_last_resort(self):
        board = [dict(BOARD[0], solved_of_ten=0)] + BOARD[1:]
        ranked = rank(board=board)
        self.assertEqual(ranked[0], ("groq", "llama-3.3-70b-versatile"))


class FallbackOrder(unittest.TestCase):
    def test_a_chosen_model_is_tried_first(self):
        chain = mr.fallback_chain(rank(), ("groq", "llama-3.3-70b-versatile"))
        self.assertEqual(chain[0], ("groq", "llama-3.3-70b-versatile"))

    def test_a_chosen_model_that_died_is_not_tried_at_all(self):
        chain = mr.fallback_chain(rank(), ("nvidia", "gone-yesterday"))
        self.assertNotIn(("nvidia", "gone-yesterday"), chain)

    def test_the_chain_holds_more_than_one_model(self):
        self.assertGreater(len(mr.fallback_chain(rank(), None)), 1)

    def test_a_rate_limited_model_is_worth_leaving_for_the_next_one(self):
        self.assertTrue(mr.is_retryable(429))
        self.assertTrue(mr.is_retryable(410))
        self.assertTrue(mr.is_retryable(None))

    def test_a_malformed_request_is_not_retried_on_another_model(self):
        self.assertFalse(mr.is_retryable(400))


class WordsForPeople(unittest.TestCase):
    def test_the_failure_message_names_no_model_and_no_provider(self):
        message = mr.all_failed_message(is_en=False)
        for jargon in ("модель", "провайдер", "лимит", "HTTP", "None"):
            self.assertNotIn(jargon.lower(), message.lower())

    def test_no_none_ever_reaches_the_wait_hint(self):
        """The 429 branch used to print the missing Retry-After header as "(None)"."""
        self.assertNotIn("None", mr.all_failed_message(retry_after_sec=None))
        self.assertIn("3", mr.all_failed_message(retry_after_sec=180))

    def test_model_ids_are_turned_into_names_people_can_read(self):
        self.assertEqual(mr.human_model_name("minimax/minimax-m3:free"), "MiniMax M3")
        self.assertEqual(mr.human_model_name("meta/llama-3.1-70b-instruct"), "Llama 3.1 70B Instruct")

    def test_a_brand_glued_to_its_version_is_still_read_as_a_brand(self):
        self.assertEqual(mr.human_model_name("qwen/qwen3.8-27b"), "Qwen 3.8 27B")
        self.assertEqual(mr.human_model_name("openai/gpt-oss-20b"), "GPT OSS 20B")

    def test_a_short_letter_and_digit_pair_is_not_split(self):
        """"m3" must not come out as "M 3"."""
        self.assertEqual(mr.human_model_name("minimax-m3"), "MiniMax M3")


if __name__ == "__main__":
    unittest.main()
