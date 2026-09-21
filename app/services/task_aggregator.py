"""
Talos Cloud — Task Aggregator.

Aggregates all UsageEvent rows for a task into a single TaskUsageSummary.
This is the user-visible record: "Task used 27 credits."

INVARIANT:
  - TaskUsageSummary never contains provider, model_id, or provider_cost_usd.
  - capability_breakdown only exposes abstract capability_ids and credit counts.
  - The total_cost_usd field exists for admin analytics only — never in user-facing responses.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.usage_event import UsageEvent
from app.models.task_summary import TaskUsageSummary


class TaskAggregator:
    """Aggregates per-call UsageEvents into a per-task TaskUsageSummary."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def finalize_task(
        self,
        task_id: str,
        account_id: uuid.UUID | None = None,
        started_at: datetime | None = None,
    ) -> TaskUsageSummary:
        """
        Aggregates all UsageEvent rows for task_id into a TaskUsageSummary.
        If a summary already exists for this task_id, updates it.
        Called when a task completes.
        """
        # Load all usage events for this task
        result = await self.db.execute(
            select(UsageEvent).where(UsageEvent.task_id == task_id)
        )
        events = list(result.scalars().all())

        # Aggregate
        total_credits = sum(e.credits_charged for e in events)
        total_cost_usd = sum(float(e.provider_cost_usd or 0) for e in events)
        pricing_version = events[0].pricing_version if events else "v1"

        # Build capability breakdown (user-visible, no provider names)
        breakdown: dict[str, int] = {}
        for event in events:
            cap = event.capability_id
            breakdown[cap] = breakdown.get(cap, 0) + int(event.credits_charged)

        # Upsert TaskUsageSummary
        existing_result = await self.db.execute(
            select(TaskUsageSummary).where(TaskUsageSummary.task_id == task_id)
        )
        summary = existing_result.scalar_one_or_none()

        if summary is None:
            summary = TaskUsageSummary(
                task_id=task_id,
                account_id=account_id,
                total_credits_charged=total_credits,
                total_cost_usd=total_cost_usd,
                capability_breakdown=breakdown,
                pricing_version=pricing_version,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )
            self.db.add(summary)
        else:
            summary.total_credits_charged = total_credits
            summary.total_cost_usd = total_cost_usd
            summary.capability_breakdown = breakdown
            summary.completed_at = datetime.now(timezone.utc)

        await self.db.flush()
        return summary

    async def get_summary(self, task_id: str) -> TaskUsageSummary | None:
        """Returns the TaskUsageSummary for a task, or None if not found."""
        result = await self.db.execute(
            select(TaskUsageSummary).where(TaskUsageSummary.task_id == task_id)
        )
        return result.scalar_one_or_none()

    def to_user_response(self, summary: TaskUsageSummary) -> dict:
        """
        Returns the user-visible task usage dict.
        NEVER includes provider, model_id, or total_cost_usd.
        """
        return {
            "task_id": summary.task_id,
            "total_credits_used": summary.total_credits_charged,
            "breakdown": summary.capability_breakdown,
            "completed_at": summary.completed_at.isoformat() if summary.completed_at else None,
        }
