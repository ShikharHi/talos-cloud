"""
Talos Cloud — Payment Gateway Base Abstraction.

Provides normalized event definitions and abstract adapter interface.
Decouples Talos billing and ledger systems from any specific payment processor.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import uuid
from typing import Any, Literal


class BillingEventType(str, Enum):
    TOPUP_COMPLETED = "topup_completed"
    SUBSCRIPTION_STARTED = "subscription_started"
    SUBSCRIPTION_RENEWED = "subscription_renewed"
    SUBSCRIPTION_PAYMENT_FAILED = "subscription_payment_failed"
    UNKNOWN = "unknown"


@dataclass
class NormalizedBillingEvent:
    event_id: str
    event_type: BillingEventType
    account_id: uuid.UUID | None
    package_type: str  # "topup" or "subscription"
    credits: int
    amount_minor: int  # in minor currency units (paise for INR, cents for USD)
    currency: str      # "INR", "USD", etc.
    reference_id: str  # order_id, payment_id, or subscription_id
    raw_payload: dict[str, Any]


@dataclass
class PaymentOrder:
    order_id: str
    gateway: str       # "razorpay" or "stripe"
    currency: str      # "INR" or "USD"
    amount_minor: int  # paise or cents
    key_id: str | None
    checkout_url: str | None
    metadata: dict[str, Any]


class BasePaymentAdapter(ABC):
    @property
    @abstractmethod
    def gateway_name(self) -> str:
        pass

    @abstractmethod
    async def create_order(
        self,
        account_id: uuid.UUID,
        package_type: str,
        credits: int,
        amount_minor: int,
        currency: str = "INR",
    ) -> PaymentOrder:
        """Create a gateway-specific order or checkout session."""
        pass

    @abstractmethod
    def verify_webhook_signature(
        self,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> bool:
        """Verify cryptographic HMAC or provider signature."""
        pass

    @abstractmethod
    def parse_webhook_event(
        self,
        payload: dict[str, Any],
    ) -> NormalizedBillingEvent:
        """Normalize gateway-specific webhook payload into NormalizedBillingEvent."""
        pass
