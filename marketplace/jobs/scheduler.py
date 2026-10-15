import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from marketplace.services.ratings import ratings_service
from marketplace.services.analytics import analytics_service

logger = logging.getLogger("talos.jobs")
scheduler = AsyncIOScheduler()

async def job_rollup_analytics():
    logger.info("[CRON] Running nightly rollup_daily_events...")
    try:
        await analytics_service.rollup_daily_events()
        logger.info("[CRON] rollup_daily_events completed successfully.")
    except Exception as e:
        logger.error(f"[CRON ERROR] rollup_daily_events failed: {e}")

async def job_reconcile_aggregates():
    logger.info("[CRON] Running nightly reconcile_aggregates...")
    try:
        await ratings_service.reconcile_aggregates()
        logger.info("[CRON] reconcile_aggregates completed successfully.")
    except Exception as e:
        logger.error(f"[CRON ERROR] reconcile_aggregates failed: {e}")

def setup_marketplace_scheduler():
    """Register cron jobs for nightly rollups and aggregate checks."""
    scheduler.add_job(job_rollup_analytics, CronTrigger(hour=2, minute=30), id="rollup_analytics")
    scheduler.add_job(job_reconcile_aggregates, CronTrigger(hour=3, minute=0), id="reconcile_aggregates")
    scheduler.start()
    return scheduler
