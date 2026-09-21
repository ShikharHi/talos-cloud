"""
Talos Cloud — Stripe Billing Integration (Stub & Webhook Handler).

Responsible for:
  - Top-up credit purchases via Checkout Session
  - Subscription tier grants and renewals
  - Idempotent webhook processing to prevent double-crediting
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import Account
from app.models.billing import BillingTransaction, StripeCustomer
from app.models.idempotency import WebhookEvent
from app.models.ledger import CreditTransaction
from app.services import ledger_service

logger = logging.getLogger(__name__)

# In-memory fast cache + fallback for tests
_PROCESSED_WEBHOOK_IDS: set[str] = set()


async def create_checkout_session(
    account_id: uuid.UUID,
    package_type: str,  # "topup" or "subscription"
    credits: int,
    amount_cents: int,
) -> dict[str, Any]:
    """
    Creates a Stripe Checkout Session (stubbed for dev/test).
    In production, uses stripe.checkout.Session.create(...).
    """
    session_id = f"cs_test_{uuid.uuid4().hex}"
    return {
        "checkout_url": f"https://checkout.stripe.com/c/pay/{session_id}",
        "session_id": session_id,
        "account_id": str(account_id),
        "package_type": package_type,
        "credits": credits,
        "amount_cents": amount_cents,
    }


async def process_stripe_webhook_event(
    event: dict[str, Any],
    db: AsyncSession,
) -> dict[str, Any]:
    """
    Processes verified Stripe webhook event payloads idempotently.
    Uses PostgreSQL WebhookEvent and unique constraints to prevent double-crediting
    across process restarts.
    """
    event_id = event.get("id")
    event_type = event.get("type")
    data_object = event.get("data", {}).get("object", {})

    if not event_id or not event_type:
        return {"status": "ignored", "reason": "invalid_event"}

    # 1. Check in-memory fast cache
    if event_id in _PROCESSED_WEBHOOK_IDS:
        logger.info(f"Duplicate webhook event {event_id} ignored (in-memory cache).")
        return {"status": "duplicate_ignored", "event_id": event_id}

    # 2. Check durable database record
    try:
        stmt = select(WebhookEvent).where(WebhookEvent.event_id == event_id)
        existing_evt = (await db.execute(stmt)).scalars().first()
        if existing_evt and existing_evt.processed:
            _PROCESSED_WEBHOOK_IDS.add(event_id)
            logger.info(f"Duplicate webhook event {event_id} ignored (database).")
            return {"status": "duplicate_ignored", "event_id": event_id}
    except Exception as e:
        logger.warning(f"Error checking WebhookEvent table: {e}")
        existing_evt = None

    payload_hash = hashlib.sha256(
        json.dumps(event, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    webhook_record = existing_evt
    if webhook_record is None:
        try:
            webhook_record = WebhookEvent(
                event_id=event_id,
                provider="stripe",
                event_type=event_type,
                payload_hash=payload_hash,
                processed=False,
            )
            db.add(webhook_record)
            await db.flush()
        except Exception as e:
            logger.warning(f"Could not insert initial WebhookEvent: {e}")

    try:
        if event_type == "checkout.session.completed":
            mode = data_object.get("mode", "payment")
            client_reference_id = data_object.get("client_reference_id")
            metadata = data_object.get("metadata", {})
            credits_str = metadata.get("credits", "0")
            credits = int(credits_str) if credits_str.isdigit() else 0
            customer_id = data_object.get("customer")
            amount_total = data_object.get("amount_total") or (credits * 1)
            currency = data_object.get("currency", "usd")

            if not client_reference_id:
                return {"status": "error", "reason": "missing_client_reference_id"}

            account_id = uuid.UUID(client_reference_id)

            # Record customer mapping if present
            if customer_id and isinstance(customer_id, str):
                try:
                    c_stmt = select(StripeCustomer).where(StripeCustomer.account_id == account_id)
                    existing_cust = (await db.execute(c_stmt)).scalars().first()
                    if not existing_cust:
                        db.add(StripeCustomer(account_id=account_id, stripe_customer_id=customer_id))
                        await db.flush()
                except Exception as ex:
                    logger.debug(f"StripeCustomer record skipped/exists: {ex}")

            if mode == "payment":
                # Top-up purchase: record payment transaction
                try:
                    b_stmt = select(BillingTransaction).where(
                        BillingTransaction.gateway == "stripe",
                        BillingTransaction.canonical_reference_id == event_id,
                    )
                    if not (await db.execute(b_stmt)).scalars().first():
                        db.add(
                            BillingTransaction(
                                gateway="stripe",
                                canonical_reference_id=event_id,
                                account_id=account_id,
                                amount_minor=amount_total,
                                currency=currency,
                                credits_granted=credits,
                                event_type=event_type,
                                payment_type="topup",
                                status="processed",
                                metadata_json=metadata,
                            )
                        )
                        await db.flush()
                except Exception as ex:
                    logger.debug(f"BillingTransaction skipped: {ex}")

                purchase_ref = f"stripe_{event_id}"
                await ledger_service.grant_topup_credits(
                    db=db,
                    account_id=account_id,
                    credits=credits,
                    purchase_ref=purchase_ref,
                )

                if webhook_record:
                    webhook_record.processed = True
                    webhook_record.processed_at = datetime.now(timezone.utc)
                _PROCESSED_WEBHOOK_IDS.add(event_id)
                await db.commit()
                return {"status": "processed", "action": "topup_granted", "credits": credits}

            elif mode == "subscription":
                # Initial subscription grant: record payment transaction
                try:
                    b_stmt = select(BillingTransaction).where(
                        BillingTransaction.gateway == "stripe",
                        BillingTransaction.canonical_reference_id == event_id,
                    )
                    if not (await db.execute(b_stmt)).scalars().first():
                        db.add(
                            BillingTransaction(
                                gateway="stripe",
                                canonical_reference_id=event_id,
                                account_id=account_id,
                                amount_minor=amount_total,
                                currency=currency,
                                credits_granted=credits,
                                event_type=event_type,
                                payment_type="subscription",
                                status="processed",
                                metadata_json=metadata,
                            )
                        )
                        await db.flush()
                except Exception as ex:
                    logger.debug(f"BillingTransaction skipped: {ex}")

                cycle_ref = f"sub_{event_id}"
                tier = metadata.get("tier", "pro")
                stmt = select(Account).where(Account.account_id == account_id)
                res = await db.execute(stmt)
                acc = res.scalar_one_or_none()
                if acc:
                    acc.subscription_tier = tier

                await ledger_service.grant_subscription_credits(
                    db=db,
                    account_id=account_id,
                    credits=credits,
                    cycle_ref=cycle_ref,
                )

                if webhook_record:
                    webhook_record.processed = True
                    webhook_record.processed_at = datetime.now(timezone.utc)
                _PROCESSED_WEBHOOK_IDS.add(event_id)
                await db.commit()
                return {"status": "processed", "action": "subscription_granted", "credits": credits}

        elif event_type == "invoice.payment_succeeded":
            # Recurring subscription renewal
            subscription_id = data_object.get("subscription")
            customer_email = data_object.get("customer_email")
            credits_str = data_object.get("metadata", {}).get("credits", "1000")
            credits = int(credits_str) if credits_str.isdigit() else 1000
            amount_total = data_object.get("amount_paid") or 0
            currency = data_object.get("currency", "usd")

            if customer_email:
                stmt = select(Account).where(Account.email == customer_email)
                res = await db.execute(stmt)
                acc = res.scalar_one_or_none()
                if acc:
                    try:
                        b_stmt = select(BillingTransaction).where(
                            BillingTransaction.gateway == "stripe",
                            BillingTransaction.canonical_reference_id == event_id,
                        )
                        if not (await db.execute(b_stmt)).scalars().first():
                            db.add(
                                BillingTransaction(
                                    gateway="stripe",
                                    canonical_reference_id=event_id,
                                    account_id=acc.account_id,
                                    amount_minor=amount_total,
                                    currency=currency,
                                    credits_granted=credits,
                                    event_type=event_type,
                                    payment_type="subscription_renewal",
                                    status="processed",
                                    metadata_json={"subscription_id": subscription_id},
                                )
                            )
                            await db.flush()
                    except Exception as ex:
                        logger.debug(f"BillingTransaction renewal skipped: {ex}")

                    await ledger_service.grant_subscription_credits(
                        db=db,
                        account_id=acc.account_id,
                        credits=credits,
                        cycle_ref=f"renewal_{event_id}",
                    )

                    if webhook_record:
                        webhook_record.processed = True
                        webhook_record.processed_at = datetime.now(timezone.utc)
                    _PROCESSED_WEBHOOK_IDS.add(event_id)
                    await db.commit()
                    return {"status": "processed", "action": "renewal_granted", "credits": credits}

        elif event_type == "invoice.payment_failed":
            logger.warning(f"Subscription payment failed for event {event_id}. Entering grace period.")
            if webhook_record:
                webhook_record.processed = True
                webhook_record.processed_at = datetime.now(timezone.utc)
            _PROCESSED_WEBHOOK_IDS.add(event_id)
            await db.commit()
            return {"status": "recorded", "action": "payment_failed"}

        return {"status": "unhandled_type", "event_type": event_type}

    except Exception as e:
        logger.error(f"Error processing webhook event {event_id}: {e}", exc_info=True)
        if webhook_record:
            webhook_record.error = str(e)
            try:
                await db.commit()
            except Exception:
                pass
        raise
