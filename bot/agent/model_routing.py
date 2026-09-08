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

# Models that answer through the claude CLI, in preference order. Being on the board is
# not enough: claude-code speaks the Anthropic Messages protocol and needs tool-calling,
# so a model that answers plain chat completions can still come back with "There's an
# issue with the selected model". Every id here was run on the server as
# `acpx --model <id> claude exec "Даров"` on 2026-09-08 and answered; nothing goes in
# without that run. Ids carry no `[1m]` — that suffix is claude-code's own 1M-context
# marker, and OpenRouter has no such model.
CLAUDE_CLI_MODELS: tuple[Candidate, ...] = (
    ("openrouter", "inclusionai/ling-3.0-flash-sante:free"),
    ("openrouter", "inclusionai/ling-3.0-flash-fin:free"),
    ("openrouter", "dots-studio/dots-3-note-preview:free"),
)

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

ACRONYMS = frozenset({"oss", "ai", "hd", "moe", "vl", "fp8", "it", "sft", "rl"})

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


def badge_keys(
    ranked: list[Candidate],
    latency: dict[Candidate, int] | None = None,
) -> dict[Candidate, str]:
    """Пометки к именам — только те, что подтверждены замером.

    Первое место в ранжировании и есть «самая стабильная»: список сортирован по
    решённым задачам, затем по доле удачных ответов. «Быстрая» — наименьшая
    задержка среди тех, кого проба успела засечь. Признака «длинные тексты» нет:
    поле contextWindow опубликованного борда пусто у всех строк (08.09.2026).
    """
    if not ranked:
        return {}
    badges: dict[Candidate, str] = {ranked[0]: "steadiest"}
    timed = [c for c in ranked if (latency or {}).get(c)]
    if timed:
        fastest = min(timed, key=lambda c: latency[c])
        badges.setdefault(fastest, "fastest")
    return badges


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


def claude_cli_candidate(
    live: set[Candidate] | frozenset[Candidate],
    preferred: Candidate | None = None,
    whitelist: tuple[Candidate, ...] = CLAUDE_CLI_MODELS,
) -> Candidate | None:
    """The pair to run the claude CLI with, or None when the mode has nothing to run on.

    A session pinned to `minimax/minimax-m3:free` sent that id into claude-code, which
    asked OpenRouter for `minimax/minimax-m3:free[1m]` and handed the error text to the
    human. Only a verified pair goes in, and only while the health probe still reaches it.
    """
    if preferred and preferred in whitelist and preferred in live:
        return preferred
    for candidate in whitelist:
        if candidate in live:
            return candidate
    return None


def engine_mode_for(
    stored_mode: str | None,
    is_admin: bool,
    claude_model: Candidate | None = None,
) -> str:
    """The mode a question is actually answered in — never what the row alone says.

    Sandboxed modes are the admin's own toy, and `claude` additionally needs a model the
    CLI can talk to. Anything unresolved reads as `native`, because native always has
    somewhere to fall.
    """
    mode = (stored_mode or "native").strip().lower()
    if mode not in ("claude", "opencode", "pi"):
        return "native"
    if not is_admin:
        return "native"
    if mode == "claude" and claude_model is None:
        return "native"
    return mode


def is_retryable(status: int | None) -> bool:
    """A busy or missing endpoint means try the next model; a bad request means stop."""
    if status is None:
        return True  # timeouts and socket errors: the next model may well answer
    return status in RETRYABLE_STATUSES or status in (404, 410, 401, 402, 403)


def _word(part: str) -> str:
    known = BRAND_NAMES.get(part.lower())
    if known:
        return known
    # "qwen3.8" is a brand glued to a version; only split on a brand of real length, or
    # "m3" would come out as "M 3".
    head = _leading_alpha(part)
    if len(head) >= 3 and head.lower() in BRAND_NAMES:
        tail = part[len(head):]
        return f"{BRAND_NAMES[head.lower()]} {tail}".strip()
    if part.lower() in ACRONYMS:
        return part.upper()
    if any(ch.isdigit() for ch in part):
        return part.upper() if len(part) <= 4 else part
    return part.capitalize()


def _leading_alpha(part: str) -> str:
    out = []
    for ch in part:
        if not ch.isalpha():
            break
        out.append(ch)
    return "".join(out)


def human_model_name(model_id: str) -> str:
    """`minimax/minimax-m3:free` → `MiniMax M3`. Nobody outside this repo speaks model ids."""
    name = (model_id or "").split("/")[-1].split(":")[0]
    words = [_word(part) for part in _NAME_SPLIT_RE.split(name) if part]
    return " ".join(w for w in words if w) or (model_id or "")


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
