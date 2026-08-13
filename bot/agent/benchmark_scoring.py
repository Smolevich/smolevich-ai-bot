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


LETTER_PATTERN = re.compile(r"ANSWER\s*[:=]\s*\(?\s*([A-J])\b", re.IGNORECASE)


def extract_letter(text: str) -> str | None:
    """Tagged multiple-choice letter; the last tag wins, as reasoning restates earlier guesses."""
    found = None
    for m in LETTER_PATTERN.finditer(text or ""):
        found = m.group(1).upper()
    return found


def score_mcq_letter(response: str, ground_truth: Any) -> tuple[bool, float, str]:
    truth = str(ground_truth or "").strip().upper()
    if not truth:
        return False, 0.0, "bad_ground_truth"
    picked = extract_letter(response)
    if picked is None:
        return False, 0.0, "no_letter_in_response"
    ok = picked == truth
    return ok, 1.0 if ok else 0.0, f"picked={picked} truth={truth}"


def _count_words(text: str) -> int:
    return len([w for w in re.split(r"\s+", (text or "").strip()) if w])


IFEVAL_CHECKS: dict[str, Any] = {
    "punctuation:no_comma": lambda t, kw: "," not in t,
    "change_case:english_lowercase": lambda t, kw: t == t.lower(),
    "change_case:english_capital": lambda t, kw: t == t.upper(),
    "startend:quotation": lambda t, kw: t.strip().startswith('"') and t.strip().endswith('"'),
    "detectable_format:title": lambda t, kw: bool(re.search(r"<<[^>]+>>", t)),
    "detectable_format:number_highlighted_sections":
        lambda t, kw: len(re.findall(r"\*[^*\n]+\*", t)) >= int(kw.get("num_highlights") or 0),
    "detectable_format:number_bullet_lists":
        lambda t, kw: len(re.findall(r"^\s*[\*\-]\s+", t, re.MULTILINE)) == int(kw.get("num_bullets") or 0),
    "detectable_content:number_placeholders":
        lambda t, kw: len(re.findall(r"\[[^\]\n]+\]", t)) >= int(kw.get("num_placeholders") or 0),
    "detectable_content:postscript":
        lambda t, kw: str(kw.get("postscript_marker") or "P.S.").lower() in t.lower(),
    "keywords:existence":
        lambda t, kw: all(str(k).lower() in t.lower() for k in (kw.get("keywords") or [])),
    "keywords:forbidden_words":
        lambda t, kw: all(str(k).lower() not in t.lower() for k in (kw.get("forbidden_words") or [])),
    "length_constraints:number_words": lambda t, kw: _words_relation(t, kw),
}


def _words_relation(text: str, kw: dict[str, Any]) -> bool:
    target = kw.get("num_words")
    if target is None:
        return True
    n = _count_words(text)
    return n <= int(target) if str(kw.get("relation") or "at least") == "less than" else n >= int(target)


def score_ifeval(response: str, ground_truth: Any) -> tuple[bool, float, str]:
    """Fraction of the row's verifiable constraints the answer actually satisfies."""
    spec = ground_truth if isinstance(ground_truth, dict) else {}
    ids = spec.get("instruction_ids") or []
    kwargs_list = spec.get("kwargs") or []
    if not ids:
        return False, 0.0, "no_constraints"
    text = response or ""
    if not text.strip():
        return False, 0.0, "empty_response"
    passed, detail = 0, []
    for i, cid in enumerate(ids):
        check = IFEVAL_CHECKS.get(cid)
        kw = kwargs_list[i] if i < len(kwargs_list) and isinstance(kwargs_list[i], dict) else {}
        if check is None:
            return False, 0.0, f"unsupported_constraint={cid}"
        try:
            good = bool(check(text, kw))
        except Exception:
            good = False
        passed += 1 if good else 0
        detail.append(f"{cid}={'ok' if good else 'fail'}")
    return passed == len(ids), passed / len(ids), ", ".join(detail)


def score(kind: str, response: str, ground_truth: Any, workspace: Path | None = None) -> tuple[bool, float, str]:
    if kind == "gsm8k_numeric":
        return score_gsm8k_numeric(response, ground_truth)
    if kind in ("math_numeric",):
        return score_gsm8k_numeric(response, ground_truth)
    if kind == "mcq_letter":
        return score_mcq_letter(response, ground_truth)
    if kind == "ifeval_constraints":
        return score_ifeval(response, ground_truth)
    if kind == "gsm8k_tooluse":
        return score_gsm8k_tooluse(workspace, ground_truth) if workspace else (False, 0.0, "no_workspace")
    return False, 0.0, f"unknown_kind={kind}"
