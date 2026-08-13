#!/usr/bin/env python3
"""Refresh small JSON samples for benchmark datasets from HuggingFace.

GSM8K only on the first iteration; new datasets get added by extending
`DATASETS`. Output is committed to the repo so the benchmark stays
deterministic and reproducible.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

DATASETS_SERVER = "https://datasets-server.huggingface.co"

DATASETS = {
    "gsm8k": {
        "filename": "gsm8k.json",
        "dataset": "openai/gsm8k",
        "config": "main",
        "split": "test",
        "license": "MIT",
        "source_url": "https://huggingface.co/datasets/openai/gsm8k",
        "sample_size": 15,
        "extract": "gsm8k",
    },
    "mmlu_pro": {
        "filename": "mmlu_pro.json",
        "dataset": "TIGER-Lab/MMLU-Pro",
        "config": "default",
        "split": "test",
        "license": "MIT",
        "source_url": "https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro",
        "sample_size": 60,
        "extract": "mmlu_pro",
    },
    "math500_int": {
        "filename": "math500_int.json",
        "dataset": "HuggingFaceH4/MATH-500",
        "config": "default",
        "split": "test",
        "license": "MIT (Hendrycks MATH)",
        "source_url": "https://huggingface.co/datasets/HuggingFaceH4/MATH-500",
        "sample_size": 40,
        "extract": "math500_int",
    },
    "ifeval": {
        "filename": "ifeval.json",
        "dataset": "google/IFEval",
        "config": "default",
        "split": "train",
        "license": "Apache-2.0",
        "source_url": "https://huggingface.co/datasets/google/IFEval",
        "sample_size": 40,
        "extract": "ifeval",
    },
}

GSM8K_ANSWER_RE = re.compile(r"####\s*(-?[\d.]+)")
INTEGER_ANSWER_RE = re.compile(r"-?\d+")
CHOICE_LETTERS = "ABCDEFGHIJ"

# Only constraints benchmark_scoring can verify with the stdlib.
IFEVAL_SUPPORTED = {
    "punctuation:no_comma",
    "change_case:english_lowercase",
    "change_case:english_capital",
    "startend:quotation",
    "detectable_format:title",
    "detectable_format:number_highlighted_sections",
    "detectable_format:number_bullet_lists",
    "detectable_content:number_placeholders",
    "detectable_content:postscript",
    "keywords:existence",
    "keywords:forbidden_words",
    "length_constraints:number_words",
}
IFEVAL_MAX_WORDS = 150


def fetch_total_rows(dataset: str, config: str, split: str) -> int:
    url = f"{DATASETS_SERVER}/info?dataset={dataset}&config={config}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        info = json.loads(resp.read().decode("utf-8"))
    splits = info.get("dataset_info", {}).get("splits", {})
    rows = int(splits.get(split, {}).get("num_examples") or 0)
    if rows <= 0:
        raise RuntimeError(f"Cannot determine row count for {dataset}/{config}/{split}: {info}")
    return rows


def fetch_rows(dataset: str, config: str, split: str, offset: int, length: int) -> list[dict[str, Any]]:
    """One block of rows. The server rate-limits bursts, so back off instead of losing samples."""
    url = (
        f"{DATASETS_SERVER}/rows?dataset={dataset}&config={config}"
        f"&split={split}&offset={offset}&length={length}"
    )
    delay = 2.0
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body.get("rows") or []
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 4:
                raise
            log.warning("429 on offset %s, waiting %.0fs", offset, delay)
            time.sleep(delay)
            delay *= 2
    return []


def extract_gsm8k(row: dict[str, Any]) -> dict[str, Any] | None:
    item = row.get("row") or {}
    question = (item.get("question") or "").strip()
    answer_raw = (item.get("answer") or "").strip()
    if not question or not answer_raw:
        return None
    m = GSM8K_ANSWER_RE.search(answer_raw)
    if not m:
        return None
    try:
        truth = float(m.group(1))
    except ValueError:
        return None
    if truth.is_integer():
        truth = int(truth)
    return {
        "id": f"gsm8k_test_{row.get('row_idx')}",
        "source": "openai/gsm8k:test",
        "question": question,
        "ground_truth": truth,
        "raw_answer": answer_raw,
    }


def extract_mmlu_pro(row: dict[str, Any]) -> dict[str, Any] | None:
    item = row.get("row") or {}
    question = (item.get("question") or "").strip()
    options = item.get("options") or []
    answer = str(item.get("answer") or "").strip().upper()
    if not question or not isinstance(options, list) or len(options) < 2:
        return None
    if answer not in CHOICE_LETTERS[:len(options)]:
        return None
    rendered = question + "\n\n" + "\n".join(
        f"{CHOICE_LETTERS[i]}) {str(opt).strip()}" for i, opt in enumerate(options))
    return {
        "id": f"mmlu_pro_test_{row.get('row_idx')}",
        "source": "TIGER-Lab/MMLU-Pro:test",
        "question": rendered,
        "ground_truth": answer,
        "category": (item.get("category") or "").strip(),
    }


def extract_math500_int(row: dict[str, Any]) -> dict[str, Any] | None:
    """Integer answers only: everything else needs symbolic comparison we cannot do."""
    item = row.get("row") or {}
    problem = (item.get("problem") or "").strip()
    answer = str(item.get("answer") or "").strip()
    if not problem or not INTEGER_ANSWER_RE.fullmatch(answer):
        return None
    return {
        "id": f"math500_test_{row.get('row_idx')}",
        "source": "HuggingFaceH4/MATH-500:test",
        "question": problem,
        "ground_truth": int(answer),
        "level": str(item.get("level") or ""),
    }


def extract_ifeval(row: dict[str, Any]) -> dict[str, Any] | None:
    """Rows whose constraints we can verify, and short enough not to burn the token budget."""
    item = row.get("row") or {}
    prompt = (item.get("prompt") or "").strip()
    ids = item.get("instruction_id_list") or []
    kwargs_list = item.get("kwargs") or []
    if not prompt or not 1 <= len(ids) <= 2:
        return None
    if any(str(i) not in IFEVAL_SUPPORTED for i in ids):
        return None
    kwargs_clean = []
    for i, cid in enumerate(ids):
        kw = kwargs_list[i] if i < len(kwargs_list) and isinstance(kwargs_list[i], dict) else {}
        kw = {k: v for k, v in kw.items() if v is not None}
        if cid == "length_constraints:number_words" and int(kw.get("num_words") or 0) >= IFEVAL_MAX_WORDS:
            return None
        kwargs_clean.append(kw)
    return {
        "id": f"ifeval_train_{row.get('row_idx')}",
        "source": "google/IFEval:train",
        "question": prompt,
        "ground_truth": {"instruction_ids": list(ids), "kwargs": kwargs_clean},
    }


EXTRACTORS = {
    "gsm8k": extract_gsm8k,
    "mmlu_pro": extract_mmlu_pro,
    "math500_int": extract_math500_int,
    "ifeval": extract_ifeval,
}


def refresh_one(name: str, cfg: dict[str, Any], out_dir: Path, seed: int) -> None:
    total = fetch_total_rows(cfg["dataset"], cfg["config"], cfg["split"])
    log.info("%s total=%s, sampling %s", name, total, cfg["sample_size"])
    rng = random.Random(seed)
    target = max(1, int(cfg["sample_size"]))
    extractor = EXTRACTORS[cfg["extract"]]
    block = int(cfg.get("block_size") or 20)
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    attempts = 0
    while len(items) < target and attempts < target * 4:
        attempts += 1
        offset = rng.randrange(0, max(1, total - block))
        try:
            rows = fetch_rows(cfg["dataset"], cfg["config"], cfg["split"], offset, block)
        except urllib.error.HTTPError as e:
            log.warning("HTTP %s on offset %s: %s", e.code, offset, e.reason)
            continue
        for row in rows:
            if len(items) >= target:
                break
            item = extractor(row)
            if item is None or item["id"] in seen_ids:
                continue
            seen_ids.add(item["id"])
            items.append(item)
        time.sleep(0.5)
    if len(items) < target:
        raise RuntimeError(f"{name}: collected only {len(items)} of {target} samples")
    payload = {
        "dataset": cfg["dataset"],
        "config": cfg["config"],
        "split": cfg["split"],
        "license": cfg["license"],
        "source_url": cfg["source_url"],
        "fetched_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "seed": seed,
        "samples": items,
    }
    out_path = out_dir / cfg["filename"]
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    log.info("Wrote %s (%s items)", out_path, len(items))


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh benchmark dataset samples")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "benchmark-datasets"))
    parser.add_argument("--seed", type=int, default=int(os.environ.get("BOT_BENCHMARK_REFRESH_SEED") or 0) or None)
    parser.add_argument("--only", action="append", default=[], help="Restrict to specific dataset names")
    args = parser.parse_args()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = args.seed if args.seed is not None else int(dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d"))
    names = args.only or list(DATASETS.keys())
    for name in names:
        if name not in DATASETS:
            log.warning("Unknown dataset %s, skipping", name)
            continue
        try:
            refresh_one(name, DATASETS[name], out_dir, seed)
        except Exception as e:
            log.error("Failed to refresh %s: %s", name, e)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
