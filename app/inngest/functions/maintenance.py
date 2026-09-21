"""
Talos Cloud — Maintenance Inngest Functions.

Provides:
  1. talos.maintenance.cleanup_stale_reservations [Scheduled: */5 * * * *]
     - Finds HELD reservations older than expires_at
     - Restores split balances (monthly + topup) to wallet

  2. talos.maintenance.cleanup_staging_uploads [Scheduled: */30 * * * *]
     - Purges abandoned staging uploads from Tigris S3
     - Updates upload statuses to EXPIRED
"""

from __future__ import annotations

import logging
from typing import Any

import inngest

from app.database import get_session_factory
from app.inngest.client import inngest_client
from app.inngest.events import TalosEvents
from app.services.recovery_worker import ReservationRecoveryWorker
from app.services.storage_cleanup import cleanup_abandoned_uploads

logger = logging.getLogger("talos.inngest.maintenance")


@inngest_client.create_function(
    fn_id="talos.maintenance.cleanup_stale_reservations",
    name="Talos Maintenance: Cleanup Stale Reservations",
    trigger=inngest.TriggerCron(cron="*/5 * * * *"),
    retries=3,
    concurrency=[
        inngest.Concurrency(
            scope="fn",
            limit=1,
        )
    ],
)
async def maintenance_cleanup_stale_reservations_fn(
    ctx: inngest.Context,
    step: inngest.Step,
) -> dict[str, Any]:
    """
    Scheduled every 5 minutes to match existing Talos reservation recovery timing.
    """
    session_factory = get_session_factory()

    async def _execute_cleanup() -> dict[str, Any]:
        async with session_factory() as session:
            cleaned = await ReservationRecoveryWorker.cleanup_stale_reservations(session)
            return {"status": "ok", "cleaned": cleaned}

    result = await step.run("cleanup-stale-reservations", _execute_cleanup)
    logger.info("Maintenance Inngest: Cleaned %s stale reservations", result.get("cleaned", 0))
    return result


@inngest_client.create_function(
    fn_id="talos.maintenance.cleanup_staging_uploads",
    name="Talos Maintenance: Cleanup Staging Uploads",
    trigger=inngest.TriggerCron(cron="*/30 * * * *"),
    retries=3,
    concurrency=[
        inngest.Concurrency(
            scope="fn",
            limit=1,
        )
    ],
)
async def maintenance_cleanup_staging_uploads_fn(
    ctx: inngest.Context,
    step: inngest.Step,
) -> dict[str, Any]:
    """
    Scheduled every 30 minutes to clean up expired staging package uploads in Tigris.
    """
    session_factory = get_session_factory()

    async def _execute_storage_cleanup() -> dict[str, Any]:
        async with session_factory() as session:
            stats = await cleanup_abandoned_uploads(session)
            return {"status": "ok", "stats": stats}

    result = await step.run("cleanup-staging-uploads", _execute_storage_cleanup)
    logger.info("Maintenance Inngest: Storage cleanup completed: %s", result.get("stats"))
    return result
