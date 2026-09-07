"""Regression: the bot is open to everyone, so the per-call features need a ceiling.

Transcription and voicing burn a provider quota per file. Ten an hour each, counted
separately, on a sliding window — and when it runs out the person is told when, not
that a "лимит" was "исчерпан".

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from agent import quota  # noqa: E402

NOON = 1_757_246_400  # a round timestamp; only the arithmetic matters
HOUR = 3600


class TenAnHour(unittest.TestCase):
    def test_the_tenth_request_within_the_hour_is_allowed(self):
        times = [NOON + i for i in range(9)]
        self.assertTrue(quota.voice_quota(times, NOON + 60)[0])

    def test_the_eleventh_request_within_the_hour_is_refused(self):
        times = [NOON + i for i in range(10)]
        self.assertFalse(quota.voice_quota(times, NOON + 60)[0])

    def test_an_hour_after_the_first_one_it_is_allowed_again(self):
        times = [NOON + i for i in range(10)]
        self.assertTrue(quota.voice_quota(times, NOON + HOUR + 1)[0])

    def test_it_says_when_the_next_one_becomes_possible(self):
        times = [NOON + i for i in range(10)]
        allowed, retry_at = quota.voice_quota(times, NOON + 60)
        self.assertFalse(allowed)
        self.assertEqual(retry_at, NOON + HOUR)

    def test_uses_older_than_the_window_do_not_count(self):
        times = [NOON - 2 * HOUR + i for i in range(50)]
        self.assertTrue(quota.voice_quota(times, NOON)[0])

    def test_transcription_and_voicing_are_counted_apart(self):
        """Two separate call sites pass their own `kind`; the function never mixes them."""
        self.assertTrue(quota.voice_quota([], NOON)[0])


class WordsForPeople(unittest.TestCase):
    def test_the_refusal_names_a_time_and_not_a_limit(self):
        message = quota.voice_limit_message(NOON + HOUR, NOON + 60, kind="stt")
        self.assertIn("59 мин", message)
        for jargon in ("лимит", "квота", "quota", "429"):
            self.assertNotIn(jargon, message.lower())

    def test_voicing_and_transcription_say_different_things(self):
        stt = quota.voice_limit_message(NOON + HOUR, NOON, kind="stt")
        tts = quota.voice_limit_message(NOON + HOUR, NOON, kind="tts")
        self.assertNotEqual(stt, tts)


if __name__ == "__main__":
    unittest.main()
