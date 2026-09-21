"""
Talos Cloud — Billing Celery Tasks (Queue: billing).

Handles subscription cycle renewals and billing reconciliations:
  - Finds all active subscriptions where next_reset_at <= NOW()
  - Applies idempotent monthly credit grants and updates billing periods
"""
import asyncio
import concurrent.futures
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app.app import celery_app
from app.database import get_session_factory
from app.models.subscription_plans import Subscription, SubscriptionStatus
from app.services.subscription_service import SubscriptionService

logger = logging.getLogger("talos.celery.billing")



def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


async def run_reconcile_subscription_cycles(db: AsyncSession | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    renewed_count = 0

    async def _process(session: AsyncSession):
        nonlocal renewed_count
        stmt = (
            select(Subscription.id)
            .where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.next_reset_at <= now,
            )
        )
        res = await session.execute(stmt)
        sub_ids = list(res.scalars().all())

        service = SubscriptionService(session)
        for s_id in sub_ids:
            try:
                success = await service.monthly_reset(s_id)
                if success:
                    renewed_count += 1
                    await session.commit()
            except Exception as e:
                logger.error("Failed to reset subscription %s: %s", s_id, e)
                await session.rollback()

    if db is not None:
        await _process(db)
    else:
        factory = get_session_factory()
        async with factory() as session:
            await _process(session)

    return {"status": "ok", "renewed": renewed_count}


@celery_app.task(name="app.celery_app.tasks.billing.reconcile_subscription_cycles_task", bind=True)
def reconcile_subscription_cycles_task(self=None, db: AsyncSession | None = None):
    """
    Daily safety net & free-tier renewal engine running on queue: billing.
    Processes due subscriptions with SELECT ... FOR UPDATE SKIP LOCKED.
    """
    result = _run_async(run_reconcile_subscription_cycles(db=db))
    logger.info("Billing: Reconciled %d subscription cycles", result.get("renewed", 0))
    return result

