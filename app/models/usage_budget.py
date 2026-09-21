"""
Talos Cloud — SubscriptionUsageBudget ORM model.

Tracks private internal USD usage budget (e.g. $5 maximum COGS budget for $20 plan).
Completely hidden from normal user APIs; used by internal risk & margin management.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Numeric, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SubscriptionUsageBudget(Base):
    """
    Per-subscription internal COGS usage budget tracker.
    """
    __tablename__ = "subscription_usage_budgets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    budget_usd: Mapped[float] = mapped_column(Numeric(10, 2), default=5.00, nullable=False)
    used_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0.00, nullable=False)
    reserved_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0.00, nullable=False)

    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
