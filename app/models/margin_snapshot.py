"""
Talos Cloud — Margin Snapshot ORM Model.

Persists historical margin metrics per capability computed by margin_monitor.
Allows the admin dashboard to inspect margins and trends.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarginSnapshot(Base):
    __tablename__ = "margin_snapshots"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    capability_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    revenue_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    margin_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )
