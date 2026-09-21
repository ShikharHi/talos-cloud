"""
Talos Cloud — Pricing Version ORM model.

One active pricing_version at a time; all historical versions retained for audit.
Changing pricing_version is a manual operation (admin CLI / internal tooling only).
The margin_monitor background job ALERTS humans when margins are unhealthy —
it never auto-publishes a new version.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PricingVersion(Base):
    """
    Versioned credit schedule. One row per published pricing version.
    `is_active` is True for exactly one row at any given time.

    When a new version is published:
      1. Set old active version's is_active = False
      2. Insert new version with is_active = True
    Both steps in one transaction.

    Historical PricingEvent rows are stamped with the version string that
    was active when they were created — they are never retroactively updated.
    """
    __tablename__ = "pricing_versions"

    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Human-readable version string, e.g. "v1", "v2"
    version: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Full YAML schedule stored as text for auditability
    schedule_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    # Who published this version (admin user email)
    published_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
