"""
Talos Cloud — Monitoring Inngest Functions.

Provides:
  1. talos.monitoring.margin_check [Scheduled: 0 2 * * *]
     - Daily 02:00 UTC check
     - Computes capability margin snapshots and logs alerts
     - INVARIANT: Never creates or publishes new pricing versions
"""

from __future__ import annotations

import logging
from typing import Any

import inngest

from app.database import get_session_factory
from app.inngest.client import inngest_client
from app.services.margin_monitor import run_margin_check

logger = logging.getLogger("talos.inngest.monitoring")


@inngest_client.create_function(
    fn_id="talos.monitoring.margin_check",
    name="Talos Monitoring: Margin Check",
    trigger=inngest.TriggerCron(cron="0 2 * * *"),
    retries=3,
    concurrency=[
        inngest.Concurrency(
            scope="fn",
            limit=1,
        )
    ],
)
async def monitoring_margin_check_fn(
    ctx: inngest.Context,
    step: inngest.Step,
) -> dict[str, Any]:
    """
    Runs daily margin snapshot calculation at 02:00 UTC.
    """
    session_factory = get_session_factory()

    async def _execute_margin_check() -> dict[str, Any]:
        async with session_factory() as session:
            try:
                await run_margin_check(session)
                await session.commit()
                return {"status": "ok", "message": "Margin check completed successfully"}
            except Exception as e:
                await session.rollback()
                logger.error("Margin check failed: %s", e, exc_info=True)
                raise

    result = await step.run("run-margin-check", _execute_margin_check)
    logger.info("Monitoring Inngest: Margin check completed")
    return result
