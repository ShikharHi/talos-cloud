"""
Talos Cloud — ProviderPricing ORM model.

Tracks real provider COGS (cost of goods sold) per model.
Prices change frequently — this table must be updated whenever
a provider publishes new pricing, not hard-coded in Python.

All rows are INTERNAL ONLY. No endpoint returns this table to clients.
Only accessible via /admin/billing/providers (admin auth required).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProviderPricing(Base):
    """
    Real provider cost per model. INTERNAL ONLY.

    Token costs are in USD per 1M tokens.
    Image costs are in USD per image (low/medium/high quality tiers).
    tool_cost_usd is per tool call if applicable.

    source_url: link to provider's official pricing page.
    source_checked_at: when we last verified these prices.
    version: human-readable version string (e.g. "anthropic-2026-08").
    """
    __tablename__ = "provider_pricing"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)

    # pricing_type: "token", "image", "call"
    pricing_type: Mapped[str] = mapped_column(String(30), nullable=False, default="token")

    # Token pricing (USD per 1M tokens)
    input_cost_usd_per_1m: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)
    output_cost_usd_per_1m: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)
    cached_input_cost_usd_per_1m: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)

    # Tool / function call pricing (per call, if applicable)
    tool_cost_usd: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)

    # Image generation pricing (USD per image, by quality tier)
    image_cost_usd_low: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)
    image_cost_usd_medium: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)
    image_cost_usd_high: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)

    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Source of pricing data
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    version: Mapped[str] = mapped_column(String(100), nullable=False, default="v1")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
