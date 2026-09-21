"""
Talos Cloud — ToolRequest ORM model.

Tracks tool call execution metering (PER_CALL, PER_UNIT, PER_SECOND, PROVIDER_COST, CUSTOM).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ToolRequest(Base):
    """
    Tool usage metering log.
    """
    __tablename__ = "tool_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    request_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    pricing_model: Mapped[str] = mapped_column(String(50), default="PER_CALL", nullable=False)  # PER_CALL, PER_UNIT, PER_SECOND, PROVIDER_COST
    usage_units: Mapped[float] = mapped_column(Numeric(12, 4), default=1.0, nullable=False)

    provider_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0.0, nullable=False)
    credit_cost: Mapped[float] = mapped_column(Numeric(10, 4), default=0.0, nullable=False)

    status: Mapped[str] = mapped_column(String(50), default="COMPLETED", nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
