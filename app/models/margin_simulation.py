"""
Talos Cloud — MarginSimulation ORM model.

Persists pricing simulator runs so admins can compare scenarios
before publishing a new capability_pricing version.

Example workflow:
  1. Admin creates simulation "Reasoning-heavy users"
  2. Sets usage_mix: {reasoning_model: 0.25, fast_model: 0.40, ...}
  3. Simulator calculates projected margin
  4. Admin compares with current baseline
  5. If margin acceptable, publishes new capability_pricing version
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, Float, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarginSimulation(Base):
    """
    Persisted result of a pricing simulator run.
    """
    __tablename__ = "margin_simulations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Pricing versions used in this simulation
    pricing_version: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_pricing_version: Mapped[str] = mapped_column(String(100), nullable=False)

    # Input: usage mix as JSON {"fast_model": 0.40, "reasoning_model": 0.25, ...}
    usage_mix: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Full simulation input/output for auditability
    simulation_input: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    simulation_output: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Projected financial outcomes
    projected_revenue: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False)
    projected_provider_cost: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False)
    projected_variable_cost: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False)
    projected_margin: Mapped[float] = mapped_column(Float, nullable=False)

    # Admin who ran this simulation
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )
