"""
Talos Cloud — Inngest Event Definitions.

Provides centralized event name constants, types, and schema helpers
for all durable workflows in Talos.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional


class TalosEvents:
    # Marketplace Events
    MARKETPLACE_PACKAGE_UPLOADED = "talos/marketplace.package.uploaded"

    # Billing Events
    BILLING_SUBSCRIPTION_RECONCILE = "talos/billing.subscription.reconcile"
    BILLING_WEBHOOK_RECEIVED = "talos/billing.webhook.received"

    # Maintenance Events
    MAINTENANCE_STALE_RESERVATIONS = "talos/maintenance.stale_reservations"
    MAINTENANCE_STAGING_CLEANUP = "talos/maintenance.staging_cleanup"

    # Auth & Notification Events
    AUTH_EMAIL_VERIFICATION = "talos/auth.email.verification"
    AUTH_EMAIL_PASSWORD_RESET = "talos/auth.email.password_reset"
    AUTH_SECURITY_ALERT = "talos/auth.security.alert"
    AUTH_CLEANUP_EXPIRED_TOKENS = "talos/auth.cleanup_expired_tokens"

    # Monitoring Events
    MONITORING_MARGIN_CHECK = "talos/monitoring.margin_check"

    # Tasks / LLM Usage Aggregation
    TASKS_USAGE_AGGREGATE = "talos/tasks.usage.aggregate"
