"""
Talos Cloud — SubscriptionPlan & Subscription ORM models.

SubscriptionPlan: defines plan tiers (free, plus, pro, pro_plus, ultra).
Plan definitions live in the DB (seeded from config/subscription_plans_seed.yaml),
never hard-coded in Python business logic.

Subscription: per-account billing period tracker.
The background worker checks next_reset_at and calls
SubscriptionService.monthly_reset() when the period ends.

Idempotency guarantee: monthly_reset must check that
current_period_start has not already been advanced before
writing any new credit grant transactions.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class SubscriptionPlan(Base):
    """
    Immutable plan definition. One row per plan tier.
    Plans are seeded from config/subscription_plans_seed.yaml.

    monthly_credits: credits granted at the start of each billing cycle.
    reset_period_days: billing cycle length (usually 30).
    topup_allowed: whether users on this plan can purchase top-up credits.
    """
    __tablename__ = "subscription_plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Stable identifier used in code: "free", "plus", "pro", "pro_plus", "ultra"
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    price_usd: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    monthly_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    reset_period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    topup_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[str] = mapped_column(String(50), nullable=False, default="v1")
    internal_usage_budget_usd: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=5.00)
    max_request_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    max_run_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    max_concurrent_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class Subscription(Base):
    """
    Per-account subscription record. One active subscription per account.

    Tracks the billing period and drives the background reset worker.

    next_reset_at: when the background worker should call monthly_reset().
    The worker queries: SELECT * FROM subscriptions WHERE next_reset_at <= NOW()
                         AND status = 'active'.

    IDEMPOTENCY: monthly_reset() checks current_period_start before granting
    credits. If it has already been advanced, the reset is skipped silently.
    """
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # one subscription per account
        index=True,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscription_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )

    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscription_status_enum"),
        default=SubscriptionStatus.ACTIVE,
        nullable=False,
        index=True,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    current_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    current_period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # The background worker fires when NOW() >= next_reset_at
    next_reset_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Optional external gateway subscription ID (Stripe/Razorpay subscription)
    gateway_subscription_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    # Deduplication tracking for billing cycles (Stripe invoice ID or cycle reference)
    last_grant_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
