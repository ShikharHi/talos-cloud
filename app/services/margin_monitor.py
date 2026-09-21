"""
Talos Cloud — Margin Monitor background job (Engine v3).

Runs on an APScheduler schedule and computes margin per capability.
When margin drops below threshold, it ALERTS humans (log / webhook).
Persists check snapshots to `margin_snapshots` for the Admin Dashboard.

INVARIANT: This job NEVER creates or publishes a new pricing_version.
The only way a new pricing_version gets published is via explicit human admin action.
This invariant is verified by:
  tests/test_margin_monitor.py::test_monitor_never_creates_pricing_version

Thresholds (70-80% blended gross margin target):
  ≥ 75%  → healthy (green)
  70-75% → acceptable (yellow)
  65-70% → warning (orange)
  < 65%  → critical (red)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from app.models.margin_snapshot import MarginSnapshot
from app.models.usage_event import UsageEvent

logger = logging.getLogger(__name__)

# Margin alert thresholds matching target economics
_HEALTHY_THRESHOLD = 0.75
_ACCEPTABLE_THRESHOLD = 0.70
_WARNING_THRESHOLD = 0.65
_CRITICAL_THRESHOLD = 0.60


async def compute_margins(db) -> dict[str, dict[str, Any]]:
    """
    Computes effective margin per capability over the last 30 days.
    First checks UsageEvent table; falls back to PricingEvent for legacy rows.
    Returns {capability_id: {...}} with metrics.
    """
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)

    # 1. Try UsageEvent table first
    result = await db.execute(
        select(
            UsageEvent.capability_id,
            func.sum(UsageEvent.credits_charged).label("total_credits"),
            func.sum(UsageEvent.provider_cost_usd).label("total_cost_usd"),
            func.count(UsageEvent.event_id).label("call_count"),
        )
        .where(UsageEvent.created_at >= thirty_days_ago)
        .group_by(UsageEvent.capability_id)
    )
    rows = list(result.all())

    # 2. Fallback to PricingEvent if UsageEvent has no data yet
    if not rows:
        from app.models.ledger import PricingEvent
        legacy_result = await db.execute(
            select(
                PricingEvent.capability_id,
                func.sum(PricingEvent.credits_charged).label("total_credits"),
                func.sum(PricingEvent.real_cost_usd).label("total_cost_usd"),
                func.count(PricingEvent.event_id).label("call_count"),
            )
            .where(
                PricingEvent.created_at >= thirty_days_ago,
                PricingEvent.rejected.is_(False),
            )
            .group_by(PricingEvent.capability_id)
        )
        rows = list(legacy_result.all())

    margins = {}
    _INTERNAL_REVENUE_PER_CREDIT = 0.10  # 10 credits = $1.00 USD internal reference

    for row in rows:
        total_credits = int(row.total_credits or 0)
        call_count = int(row.call_count or 0)
        total_cost = float(row.total_cost_usd or 0.0)
        revenue = total_credits * _INTERNAL_REVENUE_PER_CREDIT

        if revenue > 0:
            margin_ratio = (revenue - total_cost) / revenue
        else:
            margin_ratio = None

        margins[row.capability_id] = {
            "capability_id": row.capability_id,
            "call_count": call_count,
            "total_credits": total_credits,
            "total_cost_usd": total_cost,
            "revenue_usd": revenue,
            "margin_ratio": margin_ratio,
        }

    return margins


async def run_margin_check(db) -> dict[str, dict[str, Any]]:
    """
    Main check function called by APScheduler.
    Computes margins, emits alerts, and persists snapshots to MarginSnapshot table.
    NEVER creates a pricing_version.
    """
    margin_data = await compute_margins(db)

    for cap_id, data in margin_data.items():
        margin_ratio = data["margin_ratio"]
        if margin_ratio is not None:
            _emit_alert(cap_id, margin_ratio)

        snapshot = MarginSnapshot(
            capability_id=cap_id,
            call_count=data["call_count"],
            total_credits=data["total_credits"],
            total_cost_usd=data["total_cost_usd"],
            revenue_usd=data["revenue_usd"],
            margin_ratio=margin_ratio,
        )
        db.add(snapshot)

    await db.flush()
    return margin_data


async def get_persisted_margins(db) -> list[dict[str, Any]]:
    """
    Retrieves latest margin calculations for all capabilities.
    Used by Admin Dashboard GET /admin/margins.
    """
    data = await compute_margins(db)
    results = []
    for cap_id, metrics in data.items():
        ratio = metrics["margin_ratio"]
        if ratio is None:
            status = "unknown"
        elif ratio >= _HEALTHY_THRESHOLD:
            status = "healthy"
        elif ratio >= _ACCEPTABLE_THRESHOLD:
            status = "acceptable"
        elif ratio >= _WARNING_THRESHOLD:
            status = "warning"
        else:
            status = "critical"

        results.append({
            "capability_id": cap_id,
            "call_count": metrics["call_count"],
            "total_credits": metrics["total_credits"],
            "total_cost_usd": metrics["total_cost_usd"],
            "revenue_usd": metrics["revenue_usd"],
            "margin_pct": round(ratio * 100, 2) if ratio is not None else None,
            "status": status,
        })
    return results


def _emit_alert(capability_id: str, margin: float) -> None:
    """
    Emits the appropriate alert level for a capability's margin.
    INVARIANT: This function MUST NOT call any function that modifies pricing_versions table.
    """
    if margin < _CRITICAL_THRESHOLD:
        logger.critical(
            "[MARGIN ALERT CRITICAL] capability=%s margin=%.1f%% "
            "— below %.0f%% threshold. HUMAN ACTION REQUIRED to review pricing.",
            capability_id, margin * 100, _CRITICAL_THRESHOLD * 100,
        )
    elif margin < _WARNING_THRESHOLD:
        logger.error(
            "[MARGIN ALERT WARNING] capability=%s margin=%.1f%% "
            "— below %.0f%% threshold.",
            capability_id, margin * 100, _WARNING_THRESHOLD * 100,
        )
    elif margin < _ACCEPTABLE_THRESHOLD:
        logger.warning(
            "[MARGIN ALERT NOTICE] capability=%s margin=%.1f%% "
            "— below %.0f%% target.",
            capability_id, margin * 100, _ACCEPTABLE_THRESHOLD * 100,
        )
    else:
        logger.debug(
            "[MARGIN OK] capability=%s margin=%.1f%%",
            capability_id, margin * 100,
        )


def schedule_margin_monitor(scheduler) -> None:
    """Registers the margin check job with an APScheduler instance."""
    from app.database import get_session_factory

    async def _job():
        factory = get_session_factory()
        async with factory() as db:
            await run_margin_check(db)
            await db.commit()

    scheduler.add_job(
        _job,
        trigger="cron",
        hour=2,
        minute=0,
        id="margin_monitor",
        replace_existing=True,
    )
    logger.info("Margin monitor scheduled (daily 02:00 UTC)")
