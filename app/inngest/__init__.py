"""
Talos Cloud — Inngest Module.

Aggregates all Inngest functions and exports the list of functions
for serve() mounting.
"""

from app.inngest.client import inngest_client
from app.inngest.events import TalosEvents
from app.inngest.functions.marketplace import marketplace_verify_and_promote_fn
from app.inngest.functions.billing import (
    billing_reconcile_subscriptions_fn,
    billing_process_webhook_fn,
)
from app.inngest.functions.maintenance import (
    maintenance_cleanup_stale_reservations_fn,
    maintenance_cleanup_staging_uploads_fn,
)
from app.inngest.functions.monitoring import monitoring_margin_check_fn
from app.inngest.functions.tasks import tasks_aggregate_usage_fn
from app.inngest.functions.auth import (
    auth_send_verification_email_fn,
    auth_send_password_reset_email_fn,
    auth_send_security_alert_fn,
    auth_cleanup_expired_tokens_fn,
)

all_inngest_functions = [
    marketplace_verify_and_promote_fn,
    billing_reconcile_subscriptions_fn,
    billing_process_webhook_fn,
    maintenance_cleanup_stale_reservations_fn,
    maintenance_cleanup_staging_uploads_fn,
    monitoring_margin_check_fn,
    tasks_aggregate_usage_fn,
    auth_send_verification_email_fn,
    auth_send_password_reset_email_fn,
    auth_send_security_alert_fn,
    auth_cleanup_expired_tokens_fn,
]

__all__ = [
    "inngest_client",
    "TalosEvents",
    "all_inngest_functions",
    "marketplace_verify_and_promote_fn",
    "billing_reconcile_subscriptions_fn",
    "billing_process_webhook_fn",
    "maintenance_cleanup_stale_reservations_fn",
    "maintenance_cleanup_staging_uploads_fn",
    "monitoring_margin_check_fn",
    "tasks_aggregate_usage_fn",
    "auth_send_verification_email_fn",
    "auth_send_password_reset_email_fn",
    "auth_send_security_alert_fn",
    "auth_cleanup_expired_tokens_fn",
]
