"""
Talos Cloud — Multi-Gateway Billing Router.

Provides unified endpoints for creating payment orders (Razorpay INR / Stripe USD)
and receiving secure, signature-verified webhooks.
"""

from typing import Any, Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routers.relay import get_authenticated_account
from app.services.billing import (
    create_payment_order,
    get_payment_adapter,
    handle_normalized_event,
)

router = APIRouter(prefix="/billing", tags=["billing"])


class CreatePaymentOrderRequest(BaseModel):
    package_type: Literal["topup", "subscription"] = "topup"
    credits: int
    amount_minor: int | None = None
    amount_cents: int | None = None
    amount_paise: int | None = None
    currency: str = "INR"
    gateway: Literal["razorpay", "stripe"] = "razorpay"

    def get_amount_minor(self) -> int:
        if self.amount_minor is not None:
            return self.amount_minor
        if self.amount_cents is not None:
            return self.amount_cents
        if self.amount_paise is not None:
            return self.amount_paise
        return 0


class PaymentOrderResponse(BaseModel):
    order_id: str
    gateway: str
    currency: str
    amount_minor: int
    credits: int
    package_type: str
    key_id: str | None = None
    checkout_url: str | None = None
    metadata: dict[str, Any]


@router.post("/order", response_model=PaymentOrderResponse)
@router.post("/checkout-session", response_model=PaymentOrderResponse)
async def create_order_endpoint(
    req: CreatePaymentOrderRequest,
    account=Depends(get_authenticated_account),
):
    """
    Creates a payment order for the requested gateway (defaults to Razorpay for India).
    """
    order = await create_payment_order(
        account_id=account.account_id,
        package_type=req.package_type,
        credits=req.credits,
        amount_minor=req.get_amount_minor(),
        currency=req.currency,
        gateway=req.gateway,
    )
    return PaymentOrderResponse(
        order_id=order.order_id,
        gateway=order.gateway,
        currency=order.currency,
        amount_minor=order.amount_minor,
        credits=req.credits,
        package_type=req.package_type,
        key_id=order.key_id,
        checkout_url=order.checkout_url,
        metadata=order.metadata,
    )


@router.post("/webhook/razorpay")
async def razorpay_webhook_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Receives and processes verified Razorpay webhooks.
    Validates HMAC-SHA256 signature using X-Razorpay-Signature header.
    """
    body_bytes = await request.body()
    headers_dict = dict(request.headers)

    adapter = get_payment_adapter("razorpay")
    if not adapter.verify_webhook_signature(body_bytes, headers_dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Razorpay webhook signature",
        )

    try:
        import json
        payload = json.loads(body_bytes)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    normalized_event = adapter.parse_webhook_event(payload)
    return await handle_normalized_event(normalized_event, db)


@router.post("/webhook/stripe")
async def stripe_webhook_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Receives and processes verified Stripe webhooks.
    Validates signature using Stripe-Signature header.
    """
    body_bytes = await request.body()
    headers_dict = dict(request.headers)

    adapter = get_payment_adapter("stripe")
    if not adapter.verify_webhook_signature(body_bytes, headers_dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe webhook signature",
        )

    try:
        import json
        payload = json.loads(body_bytes)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    normalized_event = adapter.parse_webhook_event(payload)
    return await handle_normalized_event(normalized_event, db)


@router.post("/webhook")
async def generic_webhook_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Generic webhook dispatcher supporting both Razorpay and Stripe payloads.
    """
    body_bytes = await request.body()
    headers_dict = dict(request.headers)

    try:
        import json
        payload = json.loads(body_bytes)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    # Detect gateway from payload structure
    if "event" in payload and ("payload" in payload or "entity" in str(payload)):
        gateway = "razorpay"
    else:
        gateway = "stripe"

    adapter = get_payment_adapter(gateway)
    if not adapter.verify_webhook_signature(body_bytes, headers_dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {gateway} webhook signature",
        )

    normalized_event = adapter.parse_webhook_event(payload)
    return await handle_normalized_event(normalized_event, db)
