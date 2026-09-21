"""
Talos Cloud — Maintenance Celery Tasks (Queue: maintenance).

Handles periodic database cleanup and synchronization:
  - Expiring stale HELD reservations
  - Deleting expired staging package uploads in Tigris and marking them expired
"""

import asyncio
import concurrent.futures
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app.app import celery_app
from app.database import get_session_factory
from app.services.recovery_worker import ReservationRecoveryWorker
from app.services.storage_cleanup import cleanup_abandoned_uploads

logger = logging.getLogger("talos.celery.maintenance")


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


async def run_cleanup_stale_reservations(db: AsyncSession | None = None) -> dict[str, Any]:
    if db is not None:
        cleaned = await ReservationRecoveryWorker.cleanup_stale_reservations(db)
        return {"status": "ok", "cleaned": cleaned}

    factory = get_session_factory()
    async with factory() as session:
        cleaned = await ReservationRecoveryWorker.cleanup_stale_reservations(session)
        return {"status": "ok", "cleaned": cleaned}


async def run_cleanup_expired_staging_uploads(db: AsyncSession | None = None, storage=None) -> dict[str, Any]:
    if db is not None:
        stats = await cleanup_abandoned_uploads(db, storage=storage)
        return {"status": "ok", "stats": stats}

    factory = get_session_factory()
    async with factory() as session:
        stats = await cleanup_abandoned_uploads(session, storage=storage)
        return {"status": "ok", "stats": stats}


@celery_app.task(name="app.celery_app.tasks.maintenance.cleanup_stale_reservations_task", bind=True)
def cleanup_stale_reservations_task(self=None, db: AsyncSession | None = None):
    """
    Periodic task running every 60s.
    Inspects stale HELD reservations and releases holds.
    """
    cleaned = _run_async(run_cleanup_stale_reservations(db=db))
    logger.info("Maintenance: Cleaned %s stale reservations", cleaned.get("cleaned", 0))
    return cleaned


@celery_app.task(name="app.celery_app.tasks.maintenance.cleanup_expired_staging_uploads_task", bind=True)
def cleanup_expired_staging_uploads_task(self=None, db: AsyncSession | None = None):
    """
    Periodic task running every 2 hours.
    Cleans up abandoned uploads older than their TTL.
    """
    stats = _run_async(run_cleanup_expired_staging_uploads(db=db))
    logger.info("Maintenance: Staging cleanup completed: %s", stats.get("stats"))
    return stats
