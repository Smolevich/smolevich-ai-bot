"""A provider that shuts its door must drop out of the sweep on its own.

HuggingFace answered 402 to all ~130 models every 10 minutes from 11 May to 10 Aug 2026:
three months of full sweeps against a closed door, 1.5M log lines, noticed by nobody.

Cerebras then repeated it in the other direction: parked for 24h, unparked, swept, 402 again,
parked again — one "убран из обстрела" note to the owner every single day since 17 Aug.

Locked in here: only terminal codes (401/402/403) park a provider, 429 never does, one
live model keeps the provider in, a single successful knock brings it back, and a 402 —
a bill, not an outage — stops the probing entirely until a human turns the provider back on.

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

_spec = importlib.util.spec_from_file_location("model_health_check", _BOT_DIR / "model-health-check.py")
assert _spec and _spec.loader
mhc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mhc)

NOW = 1_800_000_000


def probes(count, *, available=False, http_code=None, message="", category="text"):
    return [mhc.ModelProbe(f"m{i}", 0, available, False, category,
                           f"HTTP Error {http_code}" if http_code else None,
                           http_code == 429, http_code, message)
            for i in range(count)]


def sweep(state, run_probes, now=NOW):
    return mhc.next_state_after_run(state, run_probes, now)


class ProviderShutdown(unittest.TestCase):
    def test_402_on_every_model_parks_the_provider_after_three_runs(self):
        state = mhc.ProviderState()
        for _ in range(3):
            state, event = sweep(state, probes(130, http_code=402, message="free tier ended"))
        self.assertEqual(event, "disabled")
        self.assertEqual(state.disabled_until, mhc.PARKED_UNTIL_HUMAN)

    def test_401_parks_for_a_day_because_a_key_can_be_fixed(self):
        state = mhc.ProviderState()
        for _ in range(3):
            state, event = sweep(state, probes(10, http_code=401))
        self.assertEqual(event, "disabled")
        self.assertEqual(state.disabled_until, NOW + 24 * 3600)

    def test_two_dead_runs_are_not_enough_to_park(self):
        state = mhc.ProviderState()
        for _ in range(2):
            state, _ = sweep(state, probes(130, http_code=402))
        self.assertEqual(state.disabled_until, 0)

    def test_the_reason_names_the_code_and_quotes_the_provider(self):
        state = mhc.ProviderState()
        for _ in range(3):
            state, _ = sweep(state, probes(4, http_code=402, message="You have exceeded your credits"))
        self.assertIn("402", state.reason)
        self.assertIn("You have exceeded your credits", state.reason)


class OverloadIsNotShutdown(unittest.TestCase):
    def test_429_forever_never_parks_the_provider(self):
        state = mhc.ProviderState()
        for _ in range(50):
            state, event = sweep(state, probes(130, http_code=429))
            self.assertEqual(event, "")
        self.assertEqual(state.disabled_until, 0)

    def test_500_on_every_model_never_parks_the_provider(self):
        state = mhc.ProviderState()
        for _ in range(10):
            state, _ = sweep(state, probes(30, http_code=503))
        self.assertEqual(state.disabled_until, 0)

    def test_network_failures_never_park_the_provider(self):
        state = mhc.ProviderState()
        for _ in range(10):
            state, _ = sweep(state, probes(30, http_code=None))
        self.assertEqual(state.disabled_until, 0)

    def test_a_terminal_minority_is_not_a_shutdown(self):
        run = probes(8, http_code=402) + probes(2, http_code=429)
        self.assertIsNone(mhc.shutdown_evidence(run))


class LiveModelsKeepTheProviderIn(unittest.TestCase):
    def test_one_live_model_among_dead_ones_keeps_the_provider_in_the_sweep(self):
        run = probes(129, http_code=402) + probes(1, available=True)
        state = mhc.ProviderState()
        for _ in range(5):
            state, event = sweep(state, run)
            self.assertEqual(event, "")
        self.assertEqual(state.disabled_until, 0)

    def test_a_good_run_clears_the_dead_streak(self):
        state = mhc.ProviderState()
        for _ in range(2):
            state, _ = sweep(state, probes(10, http_code=402))
        self.assertEqual(state.consecutive_dead_runs, 2)
        state, _ = sweep(state, probes(10, available=True))
        self.assertEqual(state.consecutive_dead_runs, 0)

    def test_a_run_that_probed_nothing_changes_nothing(self):
        state = mhc.ProviderState(0, "", 2, NOW - 600)
        after, event = sweep(state, probes(3, category="image"))
        self.assertEqual((after, event), (state, ""))


class ParkedProvider(unittest.TestCase):
    def setUp(self):
        self.parked = mhc.ProviderState(NOW + 3600, "HTTP 402 on all 130 models: no credits", 3, NOW)

    def test_a_successful_probe_returns_the_provider_to_the_sweep(self):
        state, event = mhc.next_state_after_probe(self.parked, probes(1, available=True)[0], NOW)
        self.assertEqual(event, "recovered")
        self.assertEqual((state.disabled_until, state.consecutive_dead_runs), (0, 0))

    def test_a_failed_probe_keeps_the_provider_parked_and_stays_quiet(self):
        state, event = mhc.next_state_after_probe(self.parked, probes(1, http_code=402)[0], NOW + 60)
        self.assertEqual(event, "")
        self.assertEqual(state.disabled_until, self.parked.disabled_until)

    def test_a_missing_probe_model_keeps_the_provider_parked(self):
        state, event = mhc.next_state_after_probe(self.parked, None, NOW + 60)
        self.assertEqual((state.disabled_until, event), (self.parked.disabled_until, ""))

    def test_the_admin_is_told_once_not_every_run(self):
        _, event = sweep(self.parked, probes(130, http_code=402), now=NOW + 60)
        self.assertEqual(event, "")

    def test_a_parked_provider_is_knocked_on_not_swept(self):
        self.assertEqual(mhc.run_plan(self.parked, NOW), "probe")

    def test_an_expired_park_goes_back_to_the_full_sweep(self):
        self.assertEqual(mhc.run_plan(self.parked, NOW + 7200), "sweep")


class PaymentRequiredIsNotAnOutage(unittest.TestCase):
    """Cerebras 402'd on every model from 17 Aug and re-announced itself every 24h."""

    def park(self):
        state = mhc.ProviderState()
        for _ in range(3):
            state, event = sweep(state, probes(2, http_code=402, message="Payment required"))
        return state, event

    def test_dead_provider_is_not_probed_and_not_announced(self):
        state, _ = self.park()
        for hours in (1, 25, 24 * 30):
            later = NOW + hours * 3600
            self.assertEqual(mhc.run_plan(state, later), "off", f"+{hours}h")
        after, event = sweep(state, probes(2, http_code=402), now=NOW + 25 * 3600)
        self.assertEqual(event, "")
        self.assertEqual(after.disabled_until, mhc.PARKED_UNTIL_HUMAN)

    def test_the_owner_hears_about_it_exactly_once(self):
        state, first = self.park()
        self.assertEqual(first, "disabled")
        events = [sweep(state, probes(2, http_code=402), now=NOW + h * 3600)[1] for h in range(1, 73)]
        self.assertEqual(set(events), {""})

    def test_a_human_turning_it_back_on_restores_the_sweep(self):
        state, _ = self.park()
        revived = mhc.ProviderState(0, "", 0, NOW)
        self.assertEqual(mhc.run_plan(revived, NOW), "sweep")
        self.assertEqual(mhc.run_plan(state, NOW), "off")


class RepeatedParkingStaysQuiet(unittest.TestCase):
    def test_a_second_park_for_the_same_reason_is_not_announced_again(self):
        state = mhc.ProviderState()
        for _ in range(3):
            state, event = sweep(state, probes(10, http_code=403))
        self.assertEqual(event, "disabled")
        expired = state._replace(disabled_until=NOW - 1)
        _, event = sweep(expired, probes(10, http_code=403), now=NOW)
        self.assertEqual(event, "")

    def test_a_door_that_opened_and_shut_again_is_announced_again(self):
        state = mhc.ProviderState()
        for _ in range(3):
            state, _ = sweep(state, probes(10, http_code=403))
        state, _ = sweep(state, probes(10, available=True), now=NOW + 600)
        for _ in range(3):
            state, event = sweep(state, probes(10, http_code=403), now=NOW + 1200)
        self.assertEqual(event, "disabled")


if __name__ == "__main__":
    unittest.main()
