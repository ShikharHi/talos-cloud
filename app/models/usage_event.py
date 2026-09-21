"""
Talos Cloud — UsageEvent ORM model.

One row per provider call, emitted by the relay metering adapters.
This is the lowest-level observable unit in the billing system.

CRITICAL INVARIANT: `provider` and `model_id` are INTERNAL ONLY.
They must NEVER appear in any HTTP response body sent to a client.
The only place these are used is:
  - admin billing API (/admin/billing/providers)
  - pricing_calculator.py (cost calculation)
  - margin_monitor.py (analytics)
"""

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UnitType(str, enum.Enum):
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    CACHED_TOKENS = "cached_tokens"
    REASONING_TOKENS = "reasoning_tokens"
    PER_CALL = "per_call"
    PER_MINUTE = "per_minute"
    IMAGE_LOW = "image_low"
    IMAGE_MEDIUM = "image_medium"
    IMAGE_HIGH = "image_high"


class UsageEvent(Base):
    """
    One row per atomic provider call. Emitted by metering adapters.

    Multiple UsageEvents can exist per task_id (one per provider call).
    TaskUsageSummary aggregates all events for a task into the user-visible total.

    INVARIANT: `provider` and `model_id` are strictly internal analytics fields.
    """
    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_account_created", "account_id", "created_at"),
        Index("ix_usage_events_status_created", "status", "created_at"),
        Index("ix_usage_events_account_status", "account_id", "status"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    capability_id: Mapped[str] = mapped_column(String(100), nullable=False, default="llm_proxy", index=True)

    run_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(50), default="COMPLETED", nullable=False)

    # Token counts
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reasoning_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Credits accounting
    credits_reserved: Mapped[float] = mapped_column(Numeric(10, 4), default=0, nullable=False)
    credits_released: Mapped[float] = mapped_column(Numeric(10, 4), default=0, nullable=False)

    # INTERNAL ONLY — never expose to clients
    provider: Mapped[str] = mapped_column(String(100), nullable=False, default="openai")
    model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    unit_type: Mapped[UnitType] = mapped_column(
        Enum(UnitType, name="unit_type_enum"), nullable=False, default=UnitType.INPUT_TOKENS
    )
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Real USD cost paid to provider — INTERNAL ONLY
    provider_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 8), nullable=True)

    # Credits charged to the user's wallet for this event
    credits_charged: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Arbitrary adapter-specific metadata (search_type, image_resolution, etc.)
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    pricing_version: Mapped[str] = mapped_column(String(50), nullable=False, default="v1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )
