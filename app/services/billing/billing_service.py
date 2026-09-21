"""
Talos Cloud — Unified Billing Engine.

Routes payment orders to the requested gateway adapter (defaults to Razorpay for India),
verifies webhook signatures, deduplicates webhook deliveries via database-level UNIQUE constraints,
and delegates credit grants to the provider-agnostic Credit Ledger Service.
"""

import logging
import uuid
from typing import Any
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import Account
from app.models.billing import BillingTransaction
from app.services import ledger_service
from app.services.billing.base import (
    BasePaymentAdapter,
    BillingEventType,
    NormalizedBillingEvent,
    PaymentOrder,
)
from app.services.billing.razorpay_adapter import RazorpayAdapter
from app.services.billing.stripe_adapter import StripeAdapter

logger = logging.getLogger(__name__)

# Registered payment gateway adapters
_ADAPTERS: dict[str, BasePaymentAdapter] = {
    "razorpay": RazorpayAdapter(),
    "stripe": StripeAdapter(),
}

# In-memory cache for ultra-fast transient duplicate rejection
_PROCESSED_EVENTS: set[str] = set()


def get_payment_adapter(gateway: str = "razorpay") -> BasePaymentAdapter:
    """Returns the adapter for the requested payment gateway (defaults to 'razorpay')."""
    gateway_key = gateway.lower().strip()
    adapter = _ADAPTERS.get(gateway_key)
    if not adapter:
        raise ValueError(f"Unsupported payment gateway: '{gateway}'. Supported: {list(_ADAPTERS.keys())}")
    return adapter


async def create_payment_order(
    account_id: uuid.UUID,
    package_type: str,
    credits: int,
    amount_minor: int,
    currency: str = "INR",
    gateway: str = "razorpay",
) -> PaymentOrder:
    """
    Creates a payment order using the specified gateway.
    Default gateway is Razorpay (operating in INR paise).
    """
    adapter = get_payment_adapter(gateway)
    return await adapter.create_order(
        account_id=account_id,
        package_type=package_type,
        credits=credits,
        amount_minor=amount_minor,
        currency=currency,
    )


async def handle_normalized_event(
    event: NormalizedBillingEvent,
    db: AsyncSession,
) -> dict[str, Any]:
    """
    Processes a NormalizedBillingEvent with a hard database-level UNIQUE
    constraint on (gateway, canonical_reference_id).
    """
    # 1. Fast in-memory deduplication check
    if event.event_id in _PROCESSED_EVENTS or (event.reference_id and event.reference_id in _PROCESSED_EVENTS):
        logger.info(f"Duplicate webhook event '{event.event_id}' / reference '{event.reference_id}' ignored.")
        return {"status": "duplicate_ignored", "event_id": event.event_id, "reference_id": event.reference_id}

    if not event.account_id:
        logger.warning(f"Webhook event '{event.event_id}' has no associated account_id.")
        return {"status": "error", "reason": "missing_account_id"}

    # 2. Verify account exists
    stmt = select(Account).where(Account.account_id == event.account_id)
    res = await db.execute(stmt)
    account = res.scalar_one_or_none()
    if not account:
        logger.error(f"Account '{event.account_id}' not found for billing event '{event.event_id}'.")
        return {"status": "error", "reason": "account_not_found"}

    # If this event has no credit grant (e.g. failure notification), record and return
    if event.event_type == BillingEventType.SUBSCRIPTION_PAYMENT_FAILED:
        logger.warning(f"Subscription payment failed for account {event.account_id}.")
        _PROCESSED_EVENTS.add(event.event_id)
        return {
            "status": "recorded",
            "action": "payment_failed",
            "account_id": str(event.account_id),
        }

    # 3. Database-level idempotency guarantee via BillingTransaction table
    # Resolves gateway name from currency / reference
    gateway_name = "razorpay" if (
        "rzp" in event.reference_id
        or "order_" in event.reference_id
        or "pay_" in event.reference_id
        or "INR" in event.currency
    ) else "stripe"

    canonical_ref = event.reference_id or event.event_id

    billing_record = BillingTransaction(
        gateway=gateway_name,
        canonical_reference_id=canonical_ref,
        account_id=event.account_id,
        amount_minor=event.amount_minor,
        currency=event.currency,
        credits_granted=event.credits,
        event_type=event.event_type.value,
        status="processed",
    )

    try:
        # ATOMIC TRANSACTION: Billing record insertion + Credit grant execute in the exact same DB transaction.
        # If UNIQUE constraint fails or any error occurs, all mutations roll back atomically.
        async with db.begin_nested():
            db.add(billing_record)
            await db.flush()

            if event.event_type == BillingEventType.TOPUP_COMPLETED:
                purchase_ref = f"{gateway_name}_{canonical_ref}"
                await ledger_service.grant_topup_credits(
                    db=db,
                    account_id=event.account_id,
                    credits=event.credits,
                    purchase_ref=purchase_ref,
                )
            elif event.event_type in (BillingEventType.SUBSCRIPTION_STARTED, BillingEventType.SUBSCRIPTION_RENEWED):
                cycle_ref = f"{gateway_name}_{canonical_ref}"
                account.subscription_tier = "pro"
                await ledger_service.grant_subscription_credits(
                    db=db,
                    account_id=event.account_id,
                    credits=event.credits,
                    cycle_ref=cycle_ref,
                )

    except IntegrityError:
        logger.info(
            f"Database UNIQUE constraint prevented duplicate billing transaction for {gateway_name}:{canonical_ref}. "
            "Ignoring duplicate delivery across webhook event types."
        )
        _PROCESSED_EVENTS.add(event.event_id)
        _PROCESSED_EVENTS.add(canonical_ref)
        return {
            "status": "duplicate_ignored",
            "event_id": event.event_id,
            "reference_id": canonical_ref,
            "reason": "already_credited_in_db",
        }

    # 4. Commit successful state
    _PROCESSED_EVENTS.add(event.event_id)
    _PROCESSED_EVENTS.add(canonical_ref)

    action_name = "topup_granted" if event.event_type == BillingEventType.TOPUP_COMPLETED else "subscription_granted"
    return {
        "status": "processed",
        "action": action_name,
        "credits": event.credits,
        "account_id": str(event.account_id),
    }
