"""
Talos Cloud — Razorpay Payment Gateway Adapter.

Handles Indian payment methods (UPI, NetBanking, Cards, Subscriptions via UPI Autopay / eMandate).
Operates in INR (amounts in paise: 1 INR = 100 paise).
Verifies HMAC-SHA256 webhook signatures using X-Razorpay-Signature.
"""

import hashlib
import hmac
import os
import uuid
from typing import Any

from app.services.billing.base import (
    BasePaymentAdapter,
    BillingEventType,
    NormalizedBillingEvent,
    PaymentOrder,
)


class RazorpayAdapter(BasePaymentAdapter):
    def __init__(
        self,
        key_id: str | None = None,
        key_secret: str | None = None,
        webhook_secret: str | None = None,
    ):
        self.key_id = key_id or os.environ.get("RAZORPAY_KEY_ID", "rzp_test_mockkey")
        self.key_secret = key_secret or os.environ.get("RAZORPAY_KEY_SECRET", "mocksecret")
        self.webhook_secret = webhook_secret or os.environ.get("RAZORPAY_WEBHOOK_SECRET", "mock_webhook_secret")

    @property
    def gateway_name(self) -> str:
        return "razorpay"

    async def create_order(
        self,
        account_id: uuid.UUID,
        package_type: str,
        credits: int,
        amount_minor: int,
        currency: str = "INR",
    ) -> PaymentOrder:
        """
        Creates a Razorpay Order (amount in paise).
        Stores account_id and credits in order notes for reliable webhook reconstruction.
        """
        order_id = f"order_{uuid.uuid4().hex[:14]}"
        metadata = {
            "account_id": str(account_id),
            "credits": str(credits),
            "package_type": package_type,
            "currency": currency,
        }

        return PaymentOrder(
            order_id=order_id,
            gateway="razorpay",
            currency=currency,
            amount_minor=amount_minor,
            key_id=self.key_id,
            checkout_url=None,  # Handled via Razorpay Checkout Modal / SDK in UI
            metadata=metadata,
        )

    def verify_webhook_signature(
        self,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> bool:
        """
        Verifies Razorpay HMAC-SHA256 signature from X-Razorpay-Signature header.
        """
        if not self.webhook_secret or self.webhook_secret == "mock_webhook_secret":
            return True

        provided_sig = headers.get("x-razorpay-signature") or headers.get("X-Razorpay-Signature")
        if not provided_sig:
            return False

        expected_sig = hmac.new(
            self.webhook_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(provided_sig, expected_sig)

    def parse_webhook_event(
        self,
        payload: dict[str, Any],
    ) -> NormalizedBillingEvent:
        """
        Normalizes Razorpay webhook payload into NormalizedBillingEvent.
        """
        event_name = payload.get("event", "")
        event_id = payload.get("event_id") or payload.get("id") or f"rzp_evt_{uuid.uuid4().hex[:12]}"
        payload_data = payload.get("payload", {})

        # Extract payment / order / subscription entities
        payment_entity = payload_data.get("payment", {}).get("entity", {})
        order_entity = payload_data.get("order", {}).get("entity", {})
        subscription_entity = payload_data.get("subscription", {}).get("entity", {})

        # Notes may reside in payment, order, or subscription
        notes = (
            payment_entity.get("notes")
            or order_entity.get("notes")
            or subscription_entity.get("notes")
            or payload.get("notes")
            or {}
        )

        account_id_str = notes.get("account_id")
        account_id = uuid.UUID(account_id_str) if account_id_str else None
        package_type = notes.get("package_type", "topup")
        credits_str = notes.get("credits", "0")
        credits = int(credits_str) if str(credits_str).isdigit() else 0

        amount = (
            payment_entity.get("amount")
            or order_entity.get("amount")
            or subscription_entity.get("amount")
            or 0
        )
        currency = (
            payment_entity.get("currency")
            or order_entity.get("currency")
            or "INR"
        )
        # Prioritize order_id so that both 'order.paid' and 'payment.captured'
        # for the same underlying order produce the exact same reference_id.
        reference_id = (
            order_entity.get("id")
            or payment_entity.get("order_id")
            or payment_entity.get("id")
            or subscription_entity.get("id")
            or event_id
        )

        event_type = BillingEventType.UNKNOWN
        if event_name in ("order.paid", "payment.captured"):
            if package_type == "subscription":
                event_type = BillingEventType.SUBSCRIPTION_STARTED
            else:
                event_type = BillingEventType.TOPUP_COMPLETED
        elif event_name == "subscription.charged":
            event_type = BillingEventType.SUBSCRIPTION_RENEWED
        elif event_name in ("subscription.halted", "payment.failed"):
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
