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
}

GSM8K_ANSWER_RE = re.compile(r"####\s*(-?[\d.]+)")


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
    url = (
        f"{DATASETS_SERVER}/rows?dataset={dataset}&config={config}"
        f"&split={split}&offset={offset}&length={length}"
    )
    with urllib.request.urlopen(url, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("rows") or []


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


EXTRACTORS = {"gsm8k": extract_gsm8k}


def refresh_one(name: str, cfg: dict[str, Any], out_dir: Path, seed: int) -> None:
    total = fetch_total_rows(cfg["dataset"], cfg["config"], cfg["split"])
    log.info("%s total=%s, sampling %s", name, total, cfg["sample_size"])
    rng = random.Random(seed)
    target = max(1, int(cfg["sample_size"]))
    extractor = EXTRACTORS[cfg["extract"]]
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    attempts = 0
    while len(items) < target and attempts < target * 4:
        attempts += 1
        offset = rng.randrange(0, max(1, total))
        if offset in seen:
            continue
        seen.add(offset)
        try:
            rows = fetch_rows(cfg["dataset"], cfg["config"], cfg["split"], offset, 1)
        except urllib.error.HTTPError as e:
            log.warning("HTTP %s on offset %s: %s", e.code, offset, e.reason)
            continue
        if not rows:
            continue
        item = extractor(rows[0])
        if item is not None:
            items.append(item)
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
