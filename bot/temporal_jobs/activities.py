"""Activities wrap the existing benchmark code; nothing here reimplements it.

The bot and `model-benchmark` stay stdlib-only: the dependency arrow points one way,
worker → bot code. The binary has no .py suffix, so it is loaded by path.
"""
from __future__ import annotations

import argparse
import functools
import importlib.machinery
import importlib.util
import json
import os
import sys

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .shared import BatchInput, JobOutcome, JobRef, TERMINAL_ERROR_TYPE

BENCH_PATH = os.environ.get("BOT_BENCHMARK_BIN", "/usr/local/bin/model-benchmark")
DB_FILE = os.environ.get("BOT_DB_FILE", "/var/lib/telegram-llm-bot.db")

# A missing key or a model dropped from the free tier is data, not an outage — retrying cannot help.
TERMINAL_MARKERS = ("missing_api_key", "HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404")


@functools.cache
def bench():
    """The deployed model-benchmark, imported as a module."""
    bench_dir = os.path.dirname(BENCH_PATH)
    if bench_dir not in sys.path:
        sys.path.insert(0, bench_dir)  # so its `from agent...` imports resolve
    loader = importlib.machinery.SourceFileLoader("model_benchmark", BENCH_PATH)
    spec = importlib.util.spec_from_loader("model_benchmark", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def bench_args(**overrides) -> argparse.Namespace:
    """Arguments as the CLI would build them.

    max_attempts=1 on purpose: retries belong to Temporal. With more, complete_job()
    puts the job back to 'queued' and the next attempt writes a second result row.
    """
    base = dict(
        db=DB_FILE,
        timeout=60,
        claude_timeout=120,
        max_attempts=1,
        stale_after=1800,
        provider="all",
        mode="all",
        models_per_provider=3,
        lookback_hours=168,
        batch_id="",
        jobs_retention_days=7,
        results_retention_days=30,
        tasks_path=os.environ.get("BOT_BENCHMARK_TASKS", "/etc/socks-monitor/benchmark-tasks.json"),
        datasets_dir=os.environ.get("BOT_BENCHMARK_DATASETS", "/etc/socks-monitor/benchmark-datasets"),
        methodology_path=os.environ.get("BOT_BENCHMARK_METHODOLOGY", "/etc/socks-monitor/benchmark-tasks.md"),
        benchmark_root=os.environ.get("BOT_BENCHMARK_ROOT", "/var/lib/smolevich-ai-bot/sessions/benchmarks"),
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@activity.defn
async def enqueue_batch(inp: BatchInput) -> list[JobRef]:
    mb = bench()
    batch_id = mb.enqueue_jobs(bench_args(batch_id=inp.batch_id))
    with mb.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT id, provider, mode, model_id FROM model_benchmark_jobs "
            "WHERE batch_id = ? AND status = 'queued' ORDER BY priority, id",
            (batch_id,),
        ).fetchall()
    activity.logger.info("batch %s: %s jobs", batch_id, len(rows))
    return [JobRef(id=r[0], provider=r[1], mode=r[2], model_id=r[3]) for r in rows]


def claim_one(mb, job_id: int) -> dict | None:
    """Take this job if it is still up for grabs. Guards against the cron safety net."""
    with mb.connect(DB_FILE) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, batch_id, provider, model_id, mode, task_id, sample_id, payload_json "
            "FROM model_benchmark_jobs WHERE id = ? AND status IN ('queued', 'running')",
            (job_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            "UPDATE model_benchmark_jobs SET status='running', updated_ts=?, locked_by=?, "
            "locked_ts=?, attempts=attempts+1 WHERE id=?",
            (mb.now_ts(), f"temporal:{activity.info().workflow_id}", mb.now_ts(), job_id),
        )
        conn.commit()
    payload = json.loads(row[7])
    return {
        "id": row[0], "batch_id": row[1], "provider": row[2], "model_id": row[3],
        "mode": row[4], "task_id": row[5], "sample_id": row[6],
        "task": payload["task"], "sample": payload["sample"], "attempts": 1,
    }


def execute(ref: JobRef) -> JobOutcome:
    mb = bench()
    job = claim_one(mb, ref.id)
    if job is None:
        return JobOutcome(job_id=ref.id, error="already_done")
    # run_job writes through complete_job, which upserts on job_id: a retry replaces the row.
    result = mb.run_job(bench_args(), job)
    error = str(result.get("error") or "")
    outcome = JobOutcome(
        job_id=ref.id,
        ok=bool(result.get("ok")),
        score=float(result.get("score") or 0.0),
        latency_ms=int(result.get("latency_ms") or 0),
        error=error[:300],
    )
    if error:
        terminal = any(marker in error for marker in TERMINAL_MARKERS)
        raise ApplicationError(
            error[:300],
            type=TERMINAL_ERROR_TYPE if terminal else "TransientError",
            non_retryable=terminal,
        )
    return outcome


@activity.defn
def run_native_job(ref: JobRef) -> JobOutcome:
    return execute(ref)


@activity.defn
def run_claude_job(ref: JobRef) -> JobOutcome:
    activity.heartbeat(f"claude {ref.model_id}")
    return execute(ref)


@activity.defn
def publish_leaderboard() -> dict:
    mb = bench()
    args = bench_args(limit=30, include_unbenchmarked=False)
    payload = mb.leaderboard_payload(args)
    if os.environ.get("MODEL_LEADERBOARD_TOKEN"):
        mb.publish(mb.LEADERBOARD_ENDPOINT, payload)
        mb.publish(mb.TASKS_ENDPOINT, mb.tasks_payload(args))
    return {"models": len(payload.get("models") or [])}


@activity.defn
def purge_old() -> dict:
    mb = bench()
    mb.purge(bench_args())
    return {"purged": True}
