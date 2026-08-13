"""Idempotent schedule setup: two runs a day, replacing the cron entry."""
from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleCalendarSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
)

from .shared import BatchInput, TASK_QUEUE
from .workflows import BenchmarkBatchWorkflow

SCHEDULE_ID = "smolevich-bench-2x-daily"


async def main() -> None:
    client = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"],
        namespace=os.environ["TEMPORAL_NAMESPACE"],
        api_key=os.environ["TEMPORAL_API_KEY"],
        tls=True,
    )
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            BenchmarkBatchWorkflow.run,
            BatchInput(),
            id="bench",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(minutes=60),
        ),
        spec=ScheduleSpec(calendars=[ScheduleCalendarSpec(
            hour=[ScheduleRange(7), ScheduleRange(19)],
            minute=[ScheduleRange(0)],
        )]),
        # A run lasting past the next slot means something is broken; catching up would pile on.
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=timedelta(minutes=30),
        ),
    )
    handle = client.get_schedule_handle(SCHEDULE_ID)
    with contextlib.suppress(Exception):
        await handle.delete()
    await client.create_schedule(SCHEDULE_ID, schedule, trigger_immediately=False)
    print(f"schedule {SCHEDULE_ID} set for 07:00 and 19:00 UTC")


if __name__ == "__main__":
    asyncio.run(main())
