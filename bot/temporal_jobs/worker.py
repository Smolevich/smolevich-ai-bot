"""Worker process. Two queues: chat samples run in parallel, the sandbox one does not."""
from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import enqueue_batch, publish_leaderboard, purge_old, run_claude_job, run_native_job
from .shared import CLAUDE_TASK_QUEUE, TASK_QUEUE
from .workflows import BenchmarkBatchWorkflow, ClaudeLaneWorkflow, ProviderLaneWorkflow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bench.worker")

REQUIRED_ENV = ("TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE", "TEMPORAL_API_KEY")


async def main() -> None:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"bench worker: missing env {', '.join(missing)}")
    client = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"],
        namespace=os.environ["TEMPORAL_NAMESPACE"],
        api_key=os.environ["TEMPORAL_API_KEY"],
        tls=True,
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        native = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[BenchmarkBatchWorkflow, ProviderLaneWorkflow],
            activities=[enqueue_batch, run_native_job, publish_leaderboard, purge_old],
            activity_executor=pool,
            max_concurrent_activities=6,
        )
        sandbox = Worker(
            client,
            task_queue=CLAUDE_TASK_QUEUE,
            workflows=[ClaudeLaneWorkflow],
            activities=[run_claude_job],
            activity_executor=pool,
            max_concurrent_activities=1,
        )
        log.info("polling %s and %s in %s", TASK_QUEUE, CLAUDE_TASK_QUEUE, os.environ["TEMPORAL_NAMESPACE"])
        await asyncio.gather(native.run(), sandbox.run())


if __name__ == "__main__":
    asyncio.run(main())
