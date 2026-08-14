"""Orchestration only: everything touching the network lives in activities."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .shared import (
    BatchInput,
    CLAUDE_TASK_QUEUE,
    DEFAULT_CONCURRENCY,
    DEFAULT_PAUSE_SEC,
    JobRef,
    LaneInput,
    PROVIDER_CONCURRENCY,
    PROVIDER_PAUSE_SEC,
    TERMINAL_ERROR_TYPE,
)

with workflow.unsafe.imports_passed_through():
    from .activities import (
        enqueue_batch,
        publish_leaderboard,
        purge_old,
        run_claude_job,
        run_native_job,
    )

# Free tiers meter per minute, so a retry five seconds later hits the same wall.
RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=20),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=180),
    maximum_attempts=4,
    non_retryable_error_types=[TERMINAL_ERROR_TYPE],
)


@workflow.defn
class ProviderLaneWorkflow:
    """One provider's samples, at that provider's tolerated parallelism."""

    @workflow.run
    async def run(self, inp: LaneInput) -> dict:
        width = PROVIDER_CONCURRENCY.get(inp.provider, DEFAULT_CONCURRENCY)
        pause = PROVIDER_PAUSE_SEC.get(inp.provider, DEFAULT_PAUSE_SEC)
        ok = failed = 0
        for start in range(0, len(inp.refs), width):
            if start:
                await workflow.sleep(pause)
            chunk = inp.refs[start:start + width]
            results = await asyncio.gather(*[
                workflow.execute_activity(
                    run_native_job,
                    ref,
                    start_to_close_timeout=timedelta(seconds=90),
                    schedule_to_close_timeout=timedelta(minutes=15),
                    retry_policy=RETRY,
                )
                for ref in chunk
            ], return_exceptions=True)
            for res in results:
                if isinstance(res, BaseException):
                    failed += 1
                else:
                    ok += int(res.ok)
        return {"provider": inp.provider, "ok": ok, "failed": failed}


@workflow.defn
class ClaudeLaneWorkflow:
    """Strictly sequential: there is one podman sandbox on the box."""

    @workflow.run
    async def run(self, inp: LaneInput) -> dict:
        ok = 0
        for ref in inp.refs:
            try:
                res = await workflow.execute_activity(
                    run_claude_job,
                    ref,
                    start_to_close_timeout=timedelta(seconds=300),
                    schedule_to_close_timeout=timedelta(minutes=30),
                    heartbeat_timeout=timedelta(seconds=60),
                    retry_policy=RETRY,
                )
                ok += int(res.ok)
            except Exception:  # one dead model must not abandon the rest
                continue
        return {"claude_ok": ok, "total": len(inp.refs)}


@workflow.defn
class BenchmarkBatchWorkflow:
    @workflow.run
    async def run(self, inp: BatchInput) -> dict:
        refs = await workflow.execute_activity(
            enqueue_batch,
            inp,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        lanes: dict[str, list[JobRef]] = {}
        claude: list[JobRef] = []
        for ref in refs:
            if ref.mode == "claude":
                claude.append(ref)
            else:
                lanes.setdefault(ref.provider, []).append(ref)

        children = [
            workflow.execute_child_workflow(
                ProviderLaneWorkflow.run,
                LaneInput(provider=provider, refs=refs_for_provider),
                id=f"{workflow.info().workflow_id}-{provider}",
            )
            for provider, refs_for_provider in sorted(lanes.items())
        ]
        if claude:
            children.append(workflow.execute_child_workflow(
                ClaudeLaneWorkflow.run,
                LaneInput(provider="claude", refs=claude),
                id=f"{workflow.info().workflow_id}-claude",
                task_queue=CLAUDE_TASK_QUEUE,
            ))
        summary = await asyncio.gather(*children, return_exceptions=True)

        board = {}
        if inp.publish:
            board = await workflow.execute_activity(
                publish_leaderboard,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        await workflow.execute_activity(
            purge_old,
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        return {
            "lanes": [s for s in summary if not isinstance(s, BaseException)],
            "board": board,
        }
