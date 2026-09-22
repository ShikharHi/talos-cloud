"""
Talos Cloud — Tasks Usage Aggregation Inngest Functions.

Provides:
  1. talos.tasks.aggregate_usage [Event: talos/tasks.usage.aggregate]
     - Aggregates per-call UsageEvents into TaskUsageSummary
     - INVARIANT: Never leaks provider or model_id in user-facing breakdown
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import inngest

from app.database import get_session_factory
from app.inngest.client import inngest_client
from app.inngest.events import TalosEvents
from app.services.task_aggregator import TaskAggregator

logger = logging.getLogger("talos.inngest.tasks")


@inngest_client.create_function(
    fn_id="talos.tasks.aggregate_usage",
    name="Talos Tasks: Aggregate Usage",
    trigger=inngest.TriggerEvent(event=TalosEvents.TASKS_USAGE_AGGREGATE),
    retries=3,
    concurrency=[
        inngest.Concurrency(
            key="event.data.task_id",
            limit=1,
        )
    ],
)
async def tasks_aggregate_usage_fn(
    ctx: inngest.Context,
    step: inngest.Step | None = None,
) -> dict[str, Any]:
    step = step or ctx.step
    task_id = ctx.event.data.get("task_id")
    if not task_id:
        raise inngest.NonRetriableError("Missing task_id in event data")

    account_id_str = ctx.event.data.get("account_id")
    account_uuid = uuid.UUID(account_id_str) if account_id_str else None

    session_factory = get_session_factory()

    async def _aggregate():
        async with session_factory() as session:
            aggregator = TaskAggregator(session)
            summary = await aggregator.finalize_task(
                task_id=task_id,
                account_id=account_uuid,
            )
            await session.commit()
            return aggregator.to_user_response(summary)

    result = await step.run("finalize-task-usage", _aggregate)
    return result

