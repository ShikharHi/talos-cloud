"""
Talos Cloud — Billing & Subscription Inngest Functions.

Provides:
  1. talos.billing.reconcile_subscriptions [Scheduled: */15 * * * *]
     - Finds all active subscriptions where next_reset_at <= NOW()
     - Applies idempotent monthly credit grants and updates billing periods
     - Strictly guards against double-granting

  2. talos.billing.process_webhook [Event: talos/billing.webhook.received]
     - Durable, serialized per-account webhook reconciliation
     - Uses database UNIQUE constraints to prevent duplicate ledger transactions
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import inngest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session_factory
from app.inngest.client import inngest_client
from app.inngest.events import TalosEvents
from app.models.subscription_plans import Subscription, SubscriptionStatus
from app.services.subscription_service import SubscriptionService

logger = logging.getLogger("talos.inngest.billing")


@inngest_client.create_function(
    fn_id="talos.billing.reconcile_subscriptions",
    name="Talos Billing: Reconcile Subscriptions",
    trigger=inngest.TriggerCron(cron="*/15 * * * *"),
    retries=3,
    concurrency=[
        inngest.Concurrency(
            scope="fn",
            limit=1,
        )
    ],
)
async def billing_reconcile_subscriptions_fn(
    ctx: inngest.Context,
    step: inngest.Step | None = None,
) -> dict[str, Any]:
    step = step or ctx.step
    """
    Scheduled every 15 minutes to match existing Talos Cloud subscription reset cycle.
    Idempotently grants monthly allowances for due subscriptions.
    """
    session_factory = get_session_factory()

    async def _execute_reconciliation() -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        renewed_count = 0

        async with session_factory() as session:
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

        return {"status": "ok", "renewed": renewed_count, "evaluated_at": now.isoformat()}

    result = await step.run("reconcile-subscriptions", _execute_reconciliation)
    logger.info("Billing Inngest: Reconciled %d subscription cycles", result.get("renewed", 0))
    return result


@inngest_client.create_function(
    fn_id="talos.billing.process_webhook",
    name="Talos Billing: Process Webhook Event",
    trigger=inngest.TriggerEvent(event=TalosEvents.BILLING_WEBHOOK_RECEIVED),
    retries=3,
    concurrency=[
        inngest.Concurrency(
            key="event.data.account_id",
            limit=1,
        )
    ],
)
async def billing_process_webhook_fn(
    ctx: inngest.Context,
    step: inngest.Step | None = None,
) -> dict[str, Any]:
    step = step or ctx.step
    """
    Processes asynchronous normalized billing events (e.g. from Razorpay/Stripe webhooks)
    with strict per-account serialization and DB-level UNIQUE deduplication.
    """
    event_data = ctx.event.data
    from app.services.billing.base import BillingEventType, NormalizedBillingEvent
    from app.services.billing.billing_service import handle_normalized_event

    account_id_str = event_data.get("account_id")
    import uuid
    account_uuid = uuid.UUID(account_id_str) if account_id_str else None

    normalized = NormalizedBillingEvent(
        event_id=event_data["event_id"],
        event_type=BillingEventType(event_data["event_type"]),
        account_id=account_uuid,
        amount_minor=int(event_data.get("amount_minor", 0)),
        currency=event_data.get("currency", "INR"),
        credits=int(event_data.get("credits", 0)),
        reference_id=event_data.get("reference_id"),
        raw_event=event_data.get("raw_event", {}),
    )

    session_factory = get_session_factory()

    async def _handle():
        async with session_factory() as session:
            return await handle_normalized_event(normalized, session)

    result = await step.run("handle-billing-event", _handle)
    return result

