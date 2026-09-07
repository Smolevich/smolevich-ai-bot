"""Who answers the next question, and in what order we retry.

The bot used to keep whatever model id sat in the session row forever. When NVIDIA
delisted `nvidia/nemotron-3-nano-30b-a3b` the row stayed, every request came back
HTTP 410, and the human was told to go pick another one himself. Nothing here talks
to the network or the database: callers pass rows in, ranking comes out.
"""

from __future__ import annotations

import re
from typing import Any

# PEP 695 `type` statements need 3.12; CI and dev machines still run 3.11.
Candidate = tuple[str, str]

# The health cron probes every 10 minutes and only writes rows for models the provider
# still lists, so a row untouched for an hour means the model is gone, not slow.
PROBE_STALE_AFTER_SEC = 3600

# Free tiers answer 429 in bursts; one refusal is not a dead model.
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524})

BRAND_NAMES = {
    "minimax": "MiniMax",
    "qwen": "Qwen",
    "llama": "Llama",
    "deepseek": "DeepSeek",
    "mistral": "Mistral",
    "gemma": "Gemma",
    "nemotron": "Nemotron",
    "openai": "OpenAI",
    "kimi": "Kimi",
    "glm": "GLM",
    "ling": "Ling",
    "phi": "Phi",
    "gpt": "GPT",
}

# Dots stay: they are version numbers ("llama-3.1-70b"), not word separators.
_NAME_SPLIT_RE = re.compile(r"[-_]")


def solved_of_ten(entry: dict[str, Any]) -> int | None:
    """Answers solved out of ten, or None when the model has not been measured enough."""
    if entry.get("provisional"):
        return None
    direct = entry.get("solved_of_ten")
    if direct is None:
        native = (entry.get("scores") or {}).get("native")
        if native is None:
            return None
        try:
            return int(round(float(native) * 10))
        except (TypeError, ValueError):
            return None
    try:
        return int(direct)
    except (TypeError, ValueError):
        return None


def parked_providers(state_rows: list[dict[str, Any]], now: int) -> set[str]:
    """Providers the circuit breaker took off the board (402 parks until a human returns it)."""
    parked: set[str] = set()
    for row in state_rows:
        until = int(row.get("disabled_until") or 0)
        if until < 0 or until > now:
            parked.add(str(row.get("provider") or ""))
    parked.discard("")
    return parked


def live_candidates(
    health_rows: list[dict[str, Any]],
    now: int,
    stale_after: int = PROBE_STALE_AFTER_SEC,
    parked: set[str] | frozenset[str] = frozenset(),
) -> set[Candidate]:
    """Pairs the last probe actually reached — everything else is invisible to the user."""
    live: set[Candidate] = set()
    for row in health_rows:
        provider = str(row.get("provider") or "")
        model = str(row.get("model_id") or row.get("model") or "")
        if not provider or not model or provider in parked:
            continue
        if not row.get("available"):
            continue
        if now - int(row.get("last_check") or 0) > stale_after:
            continue
        live.add((provider, model))
    return live


def success_rate(stats: dict[Candidate, tuple[int, int]], candidate: Candidate) -> float:
    """Share of requests in the window that came back without an error. No data reads as 0.5."""
    ok, total = stats.get(candidate, (0, 0))
    if total <= 0:
        return 0.5
    return ok / total


def rank_candidates(
    board: list[dict[str, Any]],
    live: set[Candidate] | frozenset[Candidate],
    stats: dict[Candidate, tuple[int, int]] | None = None,
    latency: dict[Candidate, int] | None = None,
) -> list[Candidate]:
    """Best first: measured models by answers solved, then by how often they answer at all.

    A model that solved zero of ten is not offered at all — it is measured and it failed.
    Live models the benchmark has not reached yet sit behind the measured ones, ordered by
    the same success rate, so the chain never runs dry when the board is small.
    """
    stats = stats or {}
    latency = latency or {}
    measured: list[tuple[int, float, int, str, Candidate]] = []
    seen: set[Candidate] = set()
    for entry in board:
        provider = str(entry.get("provider") or "").strip().lower()
        model = str(entry.get("model") or "").strip()
        candidate = (provider, model)
        if not provider or not model or candidate not in live or candidate in seen:
            continue
        solved = solved_of_ten(entry)
        if solved is None or solved <= 0:
            continue
        seen.add(candidate)
        measured.append((-solved, -success_rate(stats, candidate), latency.get(candidate, 0), model, candidate))
    measured.sort()

    spare: list[tuple[float, int, str, Candidate]] = []
    for candidate in live:
        if candidate in seen:
            continue
        spare.append((-success_rate(stats, candidate), latency.get(candidate, 0), candidate[1], candidate))
    spare.sort()

    return [row[-1] for row in measured] + [row[-1] for row in spare]


def pick_default(ranked: list[Candidate]) -> Candidate | None:
    """The model a person who never chose one gets. Recomputed from the ranking every time."""
    return ranked[0] if ranked else None


def fallback_chain(ranked: list[Candidate], current: Candidate | None, limit: int = 4) -> list[Candidate]:
    """What to try, in order: the current model first if it is still live, then the leaders."""
    chain: list[Candidate] = []
    if current and current in ranked:
        chain.append(current)
    for candidate in ranked:
        if len(chain) >= limit:
            break
        if candidate not in chain:
            chain.append(candidate)
    return chain


def is_retryable(status: int | None) -> bool:
    """A busy or missing endpoint means try the next model; a bad request means stop."""
    if status is None:
        return True  # timeouts and socket errors: the next model may well answer
    return status in RETRYABLE_STATUSES or status in (404, 410, 401, 402, 403)


def human_model_name(model_id: str) -> str:
    """`minimax/minimax-m3:free` → `MiniMax M3`. Nobody outside this repo speaks model ids."""
    name = (model_id or "").split("/")[-1]
    name = name.split(":")[0]
    words: list[str] = []
    for part in _NAME_SPLIT_RE.split(name):
        if not part:
            continue
        known = BRAND_NAMES.get(part.lower())
        if known:
            words.append(known)
        elif part.isdigit() or any(ch.isdigit() for ch in part):
            words.append(part.upper() if len(part) <= 4 else part)
        else:
            words.append(part.capitalize())
    return " ".join(words) or (model_id or "")


def all_failed_message(is_en: bool = False, retry_after_sec: float | None = None) -> str:
    """Said only when every candidate refused. No model ids, no providers, no HTTP codes."""
    if retry_after_sec and retry_after_sec > 0:
        minutes = max(1, int(round(retry_after_sec / 60)))
        return (f"I can't answer right now — everything is busy. Try again in about {minutes} min."
                if is_en else
                f"Сейчас не получается ответить — всё занято. Попробуй примерно через {minutes} мин.")
    return ("I can't answer right now — everything is busy. Try again in a couple of minutes."
            if is_en else
            "Сейчас не получается ответить — всё занято. Попробуй через пару минут.")
