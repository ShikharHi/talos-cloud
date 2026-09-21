"""
Talos Cloud — CapabilityPricing ORM model.

Replaces the YAML-only pricing schedule with a DB-backed table.
Allows per-capability, per-unit credit costs to be updated without
code redeployment (via admin API + pricing simulator approval flow).

One active row per (capability_id, unit) at any time.
Historical rows are retained with effective_to set.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CapabilityPricing(Base):
    """
    Credit cost definition for each capability + unit type combination.

    credit_cost: credits charged per unit. For tokens this is per 1k tokens.
    target_margin: the target gross margin this price is designed to achieve.
    pricing_version: links to PricingVersion for auditability.

    Formula used when setting credit_cost:
      credit_cost = (provider_cost_per_unit / (1 - target_margin)) / credit_reference
    where credit_reference is a server-side constant never exposed to clients.
    """
    __tablename__ = "capability_pricing"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    capability_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # e.g. "input_1k", "output_1k", "per_call", "per_minute", "image_low", "image_medium", "image_high"
    unit: Mapped[str] = mapped_column(String(50), nullable=False)

    # Credits per unit (Numeric for precision)
    credit_cost: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    # Target gross margin (e.g. 0.75 = 75%)
    target_margin: Mapped[float] = mapped_column(Float, nullable=False, default=0.75)

    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pricing_version: Mapped[str] = mapped_column(String(50), nullable=False, default="v1")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
