"""
Talos Cloud — Celery Beat Periodic Schedule Definitions.

Replaces prototype APScheduler with a single, highly-reliable Celery Beat scheduler.
"""

from celery.schedules import crontab

BEAT_SCHEDULE = {
    "maintenance-cleanup-stale-reservations": {
        "task": "app.celery_app.tasks.maintenance.cleanup_stale_reservations_task",
        "schedule": 60.0,  # Every 60 seconds
        "options": {"queue": "maintenance"},
    },
    "maintenance-cleanup-expired-staging-uploads": {
        "task": "app.celery_app.tasks.maintenance.cleanup_expired_staging_uploads_task",
        "schedule": 7200.0,  # Every 2 hours
        "options": {"queue": "maintenance"},
    },
    "billing-reconcile-subscription-cycles": {
        "task": "app.celery_app.tasks.billing.reconcile_subscription_cycles_task",
        "schedule": crontab(hour=0, minute=0),  # Daily at midnight UTC
        "options": {"queue": "billing"},
    },
}
