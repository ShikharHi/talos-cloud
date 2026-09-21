"""
Talos Cloud — Pricing Engine admin router.

Admin-only endpoints for publishing new pricing versions.

INVARIANT: Only humans (via this CLI/API) can publish a new pricing_version.
The margin_monitor background job is NEVER allowed to call these endpoints
or the underlying service functions. Tests verify this.

Endpoints require an admin secret header (TALOS_ADMIN_SECRET env var).
In production, replace with proper admin auth (internal service token).
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.pricing import PricingVersion
from app.services.pricing_engine import PricingEngine

router = APIRouter(prefix="/admin/pricing", tags=["admin-pricing"])


from app.routers.auth import require_admin as _require_admin


# ── Request / Response models ─────────────────────────────────────────────────

class PublishPricingVersionRequest(BaseModel):
    version: str          # e.g. "v2"
    schedule_yaml: str    # Full YAML content of the new schedule
    published_by: Optional[str] = None     # Admin email / username for audit trail
    effective_from: Optional[str] = None   # ISO datetime string


class PricingVersionInfo(BaseModel):
    version_id: str
    version: str
    is_active: bool
    published_by: str | None
    effective_from: str
    created_at: str


class CapabilityPriceInfo(BaseModel):
    capability_id: str
    unit: str
    credits: int
    target_margin: float
    schedule_price_example: float  # for reference only, not a billing value


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/publish", response_model=PricingVersionInfo, status_code=status.HTTP_201_CREATED)
async def publish_pricing_version(
    req: PublishPricingVersionRequest,
    db: AsyncSession = Depends(get_db),
    admin_session = Depends(_require_admin),
):
    """
    Publishes a new pricing version and makes it the active schedule.
    Previous active version is deactivated.

    This endpoint is the ONLY way to change the active pricing schedule.
    It is never called by automated systems (margin_monitor, etc.).
    """
    # Validate the YAML
    try:
        schedule_data = yaml.safe_load(req.schedule_yaml)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid YAML: {e}")

    if not isinstance(schedule_data, dict) or "capabilities" not in schedule_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Schedule YAML must have a 'capabilities' key.",
        )

    # Check version name is not already used
    existing = await db.execute(
        select(PricingVersion).where(PricingVersion.version == req.version)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Pricing version '{req.version}' already exists.",
        )

    # Deactivate all current active versions
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(PricingVersion)
        .where(PricingVersion.is_active.is_(True))
        .values(is_active=False)
    )

    # Insert new version as active
    if req.effective_from:
        effective_from = datetime.fromisoformat(req.effective_from)
        if effective_from.tzinfo is None:
            effective_from = effective_from.replace(tzinfo=timezone.utc)
    else:
        effective_from = datetime.now(timezone.utc)

    published_by = req.published_by or getattr(admin_session, "email", "admin")

    new_version = PricingVersion(
        version=req.version,
        is_active=True,
        schedule_yaml=req.schedule_yaml,
        published_by=published_by,
        effective_from=effective_from,
    )
    db.add(new_version)
    await db.flush()

    return PricingVersionInfo(
        version_id=str(new_version.version_id),
        version=new_version.version,
        is_active=new_version.is_active,
        published_by=new_version.published_by,
        effective_from=new_version.effective_from.isoformat(),
        created_at=new_version.created_at.isoformat(),
    )


@router.get("/versions", response_model=list[PricingVersionInfo])
async def list_pricing_versions(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """Lists all pricing versions, newest first."""
    result = await db.execute(
        select(PricingVersion).order_by(PricingVersion.created_at.desc())
    )
    versions = result.scalars().all()
    return [
        PricingVersionInfo(
            version_id=str(v.version_id),
            version=v.version,
            is_active=v.is_active,
            published_by=v.published_by,
            effective_from=v.effective_from.isoformat(),
            created_at=v.created_at.isoformat(),
        )
        for v in versions
    ]


@router.get("/active", response_model=PricingVersionInfo)
async def get_active_version(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
):
    """Returns the currently active pricing version."""
    result = await db.execute(
        select(PricingVersion).where(PricingVersion.is_active.is_(True))
    )
    v = result.scalar_one_or_none()
    if v is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active pricing version.")
    return PricingVersionInfo(
        version_id=str(v.version_id),
        version=v.version,
        is_active=v.is_active,
        published_by=v.published_by,
        effective_from=v.effective_from.isoformat(),
        created_at=v.created_at.isoformat(),
    )


@router.get("/compute-price/{capability_id}", response_model=CapabilityPriceInfo)
async def compute_suggested_price(
    capability_id: str,
    real_cost_per_unit: float,
    target_margin: float = 0.40,
    _: None = Depends(_require_admin),
):
    """
    Given a capability's real provider cost per unit, computes the suggested
    credit price using the margin formula:
      schedule_price = max(cost / (1 - margin), cost + floor_margin)

    This is a CALCULATION TOOL for admins setting new prices — it does not
    publish anything. The admin reviews the result and decides whether to
    call /admin/pricing/publish with the new schedule.
    """
    suggested = PricingEngine.compute_schedule_price(
        real_cost_per_unit=real_cost_per_unit,
        target_margin=target_margin,
        absolute_min_margin_per_unit=real_cost_per_unit * 0.10,
    )
    return CapabilityPriceInfo(
        capability_id=capability_id,
        unit="per_unit",
        credits=round(suggested * 1000),  # example: convert to integer credits at 1000x
        target_margin=target_margin,
        schedule_price_example=suggested,
    )
