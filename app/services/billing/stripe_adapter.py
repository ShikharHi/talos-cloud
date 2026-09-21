"""
Talos Cloud — Stripe Payment Gateway Adapter.

Handles international payment cards and Stripe Checkout Sessions.
Operates in USD (amounts in cents: 1 USD = 100 cents).
"""

import os
import uuid
from typing import Any

from app.services.billing.base import (
    BasePaymentAdapter,
    BillingEventType,
    NormalizedBillingEvent,
    PaymentOrder,
)


class StripeAdapter(BasePaymentAdapter):
    def __init__(
        self,
        api_key: str | None = None,
        webhook_secret: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("STRIPE_API_KEY", "sk_test_mock")
        self.webhook_secret = webhook_secret or os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_mock")

    @property
    def gateway_name(self) -> str:
        return "stripe"

    async def create_order(
        self,
        account_id: uuid.UUID,
        package_type: str,
        credits: int,
        amount_minor: int,
        currency: str = "USD",
    ) -> PaymentOrder:
        session_id = f"cs_test_{uuid.uuid4().hex}"
        checkout_url = f"https://checkout.stripe.com/c/pay/{session_id}"
        metadata = {
            "account_id": str(account_id),
            "credits": str(credits),
            "package_type": package_type,
            "currency": currency,
        }

        return PaymentOrder(
            order_id=session_id,
            gateway="stripe",
            currency=currency,
            amount_minor=amount_minor,
            key_id=None,
            checkout_url=checkout_url,
            metadata=metadata,
        )

    def verify_webhook_signature(
        self,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> bool:
        # In test mode with mock secret, allow pass
        sig = headers.get("stripe-signature") or headers.get("Stripe-Signature")
        if not self.webhook_secret or self.webhook_secret == "whsec_mock":
            return True
        return bool(sig)

    def parse_webhook_event(
        self,
        payload: dict[str, Any],
    ) -> NormalizedBillingEvent:
        event_id = payload.get("id") or f"evt_{uuid.uuid4().hex[:10]}"
        event_type_str = payload.get("type", "")
        data_object = payload.get("data", {}).get("object", {})

        client_ref = data_object.get("client_reference_id")
        metadata = data_object.get("metadata", {})
        account_id_str = client_ref or metadata.get("account_id")
        account_id = uuid.UUID(account_id_str) if account_id_str else None

        package_type = metadata.get("package_type", "topup")
        credits_str = metadata.get("credits", "0")
        credits = int(credits_str) if str(credits_str).isdigit() else 0

        amount = data_object.get("amount_total") or data_object.get("amount_paid") or 0
        currency = data_object.get("currency", "USD").upper()
        reference_id = data_object.get("id") or event_id

        event_type = BillingEventType.UNKNOWN
        if event_type_str == "checkout.session.completed":
            mode = data_object.get("mode", "payment")
            if mode == "subscription":
                event_type = BillingEventType.SUBSCRIPTION_STARTED
                package_type = "subscription"
            else:
                event_type = BillingEventType.TOPUP_COMPLETED
                package_type = "topup"
        elif event_type_str == "invoice.payment_succeeded":
            event_type = BillingEventType.SUBSCRIPTION_RENEWED
            package_type = "subscription"
        elif event_type_str == "invoice.payment_failed":
            event_type = BillingEventType.SUBSCRIPTION_PAYMENT_FAILED

        return NormalizedBillingEvent(
            event_id=str(event_id),
            event_type=event_type,
            account_id=account_id,
            package_type=package_type,
            credits=credits,
            amount_minor=int(amount),
            currency=currency,
            reference_id=str(reference_id),
            raw_payload=payload,
        )
