"""Ceilings on the two features that cost a fixed amount per call.

Text has no ceiling of ours: the free tiers and the breaker already are one, and a
number invented on top would only stop people asking questions. Transcription and
voicing burn a provider quota per file regardless of length, so they get ten an hour
each, counted separately.
"""

from __future__ import annotations

VOICE_LIMIT_PER_HOUR = 10
VOICE_WINDOW_SEC = 3600


def voice_quota(times: list[int], now: int,
                limit: int = VOICE_LIMIT_PER_HOUR,
                window: int = VOICE_WINDOW_SEC) -> tuple[bool, int]:
    """(allowed, when the next one becomes possible) from this person's recent uses.

    A sliding window, not a calendar hour: the oldest use inside the window is what
    expires first, so waiting always helps and nothing unlocks in a burst at :00.
    """
    recent = sorted(t for t in times if t > now - window)
    if len(recent) < limit:
        return True, 0
    return False, recent[len(recent) - limit] + window


def voice_limit_message(retry_at: int, now: int, kind: str = "stt", is_en: bool = False) -> str:
    """Says when, not why. "Лимит исчерпан" tells a person nothing they can act on."""
    minutes = max(1, int(round((retry_at - now) / 60)))
    if kind == "tts":
        return (f"Enough voicing for now — the next one in about {minutes} min."
                if is_en else
                f"Озвучек пока хватит — следующая примерно через {minutes} мин.")
    return (f"Enough transcribing for now — the next one in about {minutes} min."
            if is_en else
            f"Расшифровок пока хватит — следующая примерно через {minutes} мин.")
