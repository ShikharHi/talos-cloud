"""
Talos Cloud — Background Worker (APScheduler jobs).

Runs three periodic background jobs:
  1. subscription_reset_job  [every 15 min] — finds subscriptions due for monthly reset
  2. reservation_expiry_job  [every 5 min]  — releases stale HELD credit reservations
  3. margin_check_job        [daily 02:00]  — computes margins and emits alerts

All jobs are idempotent and safe to retry.

INVARIANT: No job here publishes new pricing versions.
Human admin action is the only path to publishing new capability_pricing.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def subscription_reset_job(session_factory) -> None:
    """
    Finds all subscriptions whose next_reset_at has passed and resets them.
    Idempotent: SubscriptionService.monthly_reset() guards against double-grant.
    """
    from app.services.subscription_service import SubscriptionService

    async with session_factory() as db:
        try:
            svc = SubscriptionService(db)
            due_subs = await svc.get_subscriptions_due_for_reset()

            reset_count = 0
            for sub in due_subs:
                try:
                    was_reset = await svc.monthly_reset(sub.id)
                    if was_reset:
                        reset_count += 1
                except Exception as e:
                    logger.error(
                        "Failed to reset subscription %s: %s", sub.id, e, exc_info=True
                    )

            await db.commit()
            if reset_count > 0:
                logger.info("Subscription reset job: reset %d subscriptions.", reset_count)
        except Exception as e:
            await db.rollback()
            logger.error("Subscription reset job failed: %s", e, exc_info=True)


async def reservation_expiry_job(session_factory) -> None:
    """
    Finds HELD credit reservations past their expires_at and releases them.
    Restores wallet balance for each expired reservation.
    """
    from sqlalchemy import select
    from app.models.wallet import CreditReservation, ReservationStatus
    from app.services.wallet_engine import WalletEngine

    async with session_factory() as db:
        try:
            now = datetime.now(timezone.utc)
            result = await db.execute(
                select(CreditReservation).where(
                    CreditReservation.status == ReservationStatus.HELD,
                    CreditReservation.expires_at <= now,
                )
            )
            stale = list(result.scalars().all())

            released_count = 0
            engine = WalletEngine(db)
            for reservation in stale:
                try:
                    await engine.release(reservation.reservation_id)
                    released_count += 1
                except Exception as e:
                    logger.error(
                        "Failed to release expired reservation %s: %s",
                        reservation.reservation_id, e, exc_info=True,
                    )

            await db.commit()
            if released_count > 0:
                logger.info(
                    "Reservation expiry job: released %d stale reservations.", released_count
                )
        except Exception as e:
            await db.rollback()
            logger.error("Reservation expiry job failed: %s", e, exc_info=True)


async def margin_check_job(session_factory) -> None:
    """
    Computes margins per capability and emits alerts.
    Persists MarginSnapshot rows. NEVER creates pricing versions.
    """
    from app.services.margin_monitor import run_margin_check

    async with session_factory() as db:
        try:
            await run_margin_check(db)
            await db.commit()
            logger.info("Margin check job completed.")
        except Exception as e:
            await db.rollback()
            logger.error("Margin check job failed: %s", e, exc_info=True)


async def storage_cleanup_job(session_factory) -> None:
    """
    Finds abandoned/expired package uploads and deletes their staging objects from storage.
    """
    from app.services.storage_cleanup import cleanup_abandoned_uploads

    async with session_factory() as db:
        try:
            stats = await cleanup_abandoned_uploads(db)
            if stats.get("expired_marked", 0) > 0:
                logger.info("Storage cleanup job: purged %s expired uploads", stats["expired_marked"])
        except Exception as e:
            logger.error("Storage cleanup job failed: %s", e, exc_info=True)


def schedule_background_jobs(scheduler, session_factory) -> None:
    """
    Registers all background jobs with APScheduler.
    Call from app startup (main.py lifespan).
    """

    async def _reset_job():
        await subscription_reset_job(session_factory)

    async def _expiry_job():
        await reservation_expiry_job(session_factory)

    async def _margin_job():
        await margin_check_job(session_factory)

    async def _storage_cleanup():
        await storage_cleanup_job(session_factory)

    # Every 15 minutes
    scheduler.add_job(
        _reset_job,
        trigger="interval",
        minutes=15,
        id="subscription_reset",
        replace_existing=True,
    )

    # Every 5 minutes
    scheduler.add_job(
        _expiry_job,
        trigger="interval",
        minutes=5,
        id="reservation_expiry",
        replace_existing=True,
    )

    # Every 30 minutes
    scheduler.add_job(
        _storage_cleanup,
        trigger="interval",
        minutes=30,
        id="storage_cleanup",
        replace_existing=True,
    )

    # Daily at 02:00 UTC
    scheduler.add_job(
        _margin_job,
        trigger="cron",
        hour=2,
        minute=0,
        id="margin_check",
        replace_existing=True,
    )

    logger.info(
        "Background jobs scheduled: subscription_reset (15min), "
        "reservation_expiry (5min), storage_cleanup (30min), margin_check (daily 02:00 UTC)"
    )
