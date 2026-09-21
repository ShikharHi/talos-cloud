"""
Talos Cloud — Task Usage Router.

Endpoints for retrieving user-facing per-task credit spend summaries.

CRITICAL INVARIANT: No endpoint in this router returns provider names,
model_id, or internal USD costs to clients.

Endpoints:
  GET /tasks/{task_id}/usage        → returns total credits + capability breakdown
  GET /tasks/{task_id}/usage/detail → returns itemized capability usage
"""

import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routers.relay import get_authenticated_account
from app.services.task_aggregator import TaskAggregator

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskUsageResponse(BaseModel):
    task_id: str
    total_credits_used: int
    breakdown: dict[str, int]
    completed_at: str | None = None


@router.get("/{task_id}/usage", response_model=TaskUsageResponse)
async def get_task_usage(
    task_id: str,
    account=Depends(get_authenticated_account),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns user-facing credit summary for a completed task.
    Shows total credits charged and per-capability credit breakdown.
    NEVER leaks provider names or USD cost.
    """
    aggregator = TaskAggregator(db)
    summary = await aggregator.get_summary(task_id)

    if summary is None:
        # Aggregation may not have been run yet — run it live
        summary = await aggregator.finalize_task(
            task_id=task_id,
            account_id=account.account_id,
        )

    # Security check: verify task belongs to authenticated user
    if summary.account_id and summary.account_id != account.account_id and account.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this task's usage data.",
        )

    return TaskUsageResponse(**aggregator.to_user_response(summary))


@router.get("/{task_id}/usage/detail")
async def get_task_usage_detail(
    task_id: str,
    account=Depends(get_authenticated_account),
    db: AsyncSession = Depends(get_db),
):
    """
    Detailed capability usage itemization for a task.
    Shows abstract capability_id, credit_charge, and timestamp.
    NEVER leaks provider or model_id.
    """
    from sqlalchemy import select
    from app.models.usage_event import UsageEvent

    result = await db.execute(
        select(UsageEvent).where(
            UsageEvent.task_id == task_id,
            UsageEvent.account_id == account.account_id,
        ).order_by(UsageEvent.created_at.asc())
    )
    events = result.scalars().all()

    if not events and account.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No usage events found for task '{task_id}'.",
        )

    detail = [
        {
            "capability_id": event.capability_id,
            "unit_type": event.unit_type.value,
            "quantity": event.quantity,
            "credits_charged": int(event.credits_charged),
            "timestamp": event.created_at.isoformat(),
        }
        for event in events
    ]

    return {
        "task_id": task_id,
        "total_events": len(detail),
        "total_credits": sum(e["credits_charged"] for e in detail),
        "events": detail,
    }
