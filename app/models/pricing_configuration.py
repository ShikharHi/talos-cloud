"""
Talos Cloud — PricingConfiguration ORM Model.

Database-backed financial calibration settings, including credit_reference_usd.
All modifications are immutable and versioned with effective_from / effective_to timestamps.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PricingConfiguration(Base):
    """
    Global system financial configuration.
    Defines the baseline monetary reference value of 1 credit.
    """
    __tablename__ = "pricing_configurations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Monetary reference: USD value of 1 credit (e.g. Decimal("0.10000000"))
    credit_reference_usd: Mapped[float] = mapped_column(
        Numeric(18, 8), nullable=False, default=0.10000000
    )
    version: Mapped[str] = mapped_column(String(50), nullable=False, default="v1")
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
