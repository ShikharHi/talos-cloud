"""
Talos Cloud — TaskUsageSummary ORM model.

Aggregated per-task credit and cost record.
This is what the USER sees — a single number (e.g. "Task used 27 credits").

The underlying UsageEvent rows contain full provider/cost detail for admin use.
TaskUsageSummary hides all internal economics and presents only:
  - total_credits_charged (user-facing)
  - capability_breakdown: {"reasoning_model": 11, "web_search": 8, "code_model": 8}

Do NOT include provider, model_id, or cost fields in this model's API responses.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskUsageSummary(Base):
    """
    User-visible per-task credit summary.

    capability_breakdown: JSON dict mapping abstract capability_id → credits used.
    Example: {"reasoning_model": 11, "web_search": 8, "code_model": 8}

    This table is written by TaskAggregator.finalize_task() after task completion.
    """
    __tablename__ = "task_usage_summary"

    # task_id is the primary key — one summary per task
    task_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # User-facing total credits consumed by this task
    total_credits_charged: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Admin-only: real USD cost (never expose to users)
    total_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 8), nullable=True)

    # User-visible breakdown: {capability_id: credits}
    capability_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    pricing_version: Mapped[str] = mapped_column(String(50), nullable=False, default="v1")

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )
