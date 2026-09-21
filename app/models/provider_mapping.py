"""
Talos Cloud — ProviderMapping ORM model.

Maps abstract capability_id → concrete (provider, model_id).
The relay and model registry look up this table to dispatch calls.

This allows changing the underlying model for any capability
without touching UI code, subscriptions, wallet, or ledger.

Example:
  capability_id="reasoning_model" → provider="anthropic", model_id="claude-3-5-sonnet-20241022"
  capability_id="fast_model"      → provider="groq",      model_id="llama-3.3-70b-versatile"

All fields are INTERNAL ONLY. Never expose provider/model_id to clients.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProviderMapping(Base):
    """
    Capability → provider/model routing table.

    priority: lower number = preferred. Used when multiple rows exist
    for the same capability_id (e.g. primary + fallback provider).
    active: only active rows are used for routing.
    """
    __tablename__ = "provider_mapping"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    capability_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    # INTERNAL ONLY — never expose to clients
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)

    # Lower priority number = preferred route
    priority: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
