"""Deterministic scorers for benchmark tasks.

Only auto-scoring — no LLM judge. Each scorer returns `(ok, score, detail)`:
- ok    : whether the response counts as fully correct
- score : float in [0.0, 1.0] (partial credit allowed)
- detail: short string for logs / details_json
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

# Only tags the task explicitly asks for. Loose fallbacks used to fire on 30% of
# responses and were right 11.7% of the time — 5 points of accuracy out of thin air,
# handed to models that ignored the format.
NUMERIC_PATTERNS = [
    re.compile(r"\\boxed\s*\{\s*(-?[\d]+(?:\.[\d]+)?)\s*\}"),
    re.compile(r"ANSWER\s*[:=]\s*(-?[\d]+(?:\.[\d]+)?)", re.IGNORECASE),
]


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_numeric(text: str) -> float | None:
    cleaned = (text or "").replace(",", "").strip()
    # Last match wins: reasoning restates numbers, the tagged answer comes last.
    found = None
    for pat in NUMERIC_PATTERNS:
        for m in pat.finditer(cleaned):
            f = _to_float(m.group(1))
            if f is not None:
                found = f
    return found


def score_gsm8k_numeric(response: str, ground_truth: Any) -> tuple[bool, float, str]:
    truth = _to_float(ground_truth)
    if truth is None:
        return False, 0.0, "bad_ground_truth"
    extracted = extract_numeric(response)
    if extracted is None:
        return False, 0.0, "no_number_in_response"
    ok = math.isfinite(extracted) and abs(extracted - truth) < 1e-3
    return ok, 1.0 if ok else 0.0, f"extracted={extracted} truth={truth}"


def score_gsm8k_tooluse(workspace: Path, ground_truth: Any) -> tuple[bool, float, str]:
    if workspace is None or not workspace.exists():
        return False, 0.0, "no_workspace"
    scratch = workspace / "scratch.md"
    answer = workspace / "answer.txt"
    score = 0.0
    parts = []
    if scratch.exists() and scratch.read_text(encoding="utf-8", errors="replace").strip():
        score += 0.5
        parts.append("scratch=ok")
    else:
        parts.append("scratch=missing")
    if answer.exists():
        answer_text = answer.read_text(encoding="utf-8", errors="replace")
        extracted = extract_numeric(answer_text)
        truth = _to_float(ground_truth)
        if extracted is not None and truth is not None and abs(extracted - truth) < 1e-3:
            score += 0.5
            parts.append(f"answer={extracted}")
        else:
            parts.append(f"answer_bad={answer_text.strip()[:40]!r}")
    else:
        parts.append("answer=missing")
    ok = score >= 1.0 - 1e-9
    return ok, score, ", ".join(parts)


def score(kind: str, response: str, ground_truth: Any, workspace: Path | None = None) -> tuple[bool, float, str]:
    if kind == "gsm8k_numeric":
        return score_gsm8k_numeric(response, ground_truth)
    if kind == "gsm8k_tooluse":
        return score_gsm8k_tooluse(workspace, ground_truth) if workspace else (False, 0.0, "no_workspace")
    return False, 0.0, f"unknown_kind={kind}"
