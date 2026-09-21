"""
Talos Cloud — User Dashboard Router (Phase 10b).

Endpoints for user credit balance, capability usage breakdown, task costs, and credit purchases.

CRITICAL SECURITY INVARIANTS:
  1. All routes derive the user account strictly from the verified server-side WebSession.
     Client-supplied account_id params/body fields are NEVER accepted or trusted.
  2. The 'provider' field in PricingEvent MUST NEVER appear anywhere in any response payload.
     All usage is expressed in abstract capabilities with user-facing labels.
"""

from typing import Any, Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.accounts import Account
from app.models.ledger import PricingEvent
from app.routers.auth import get_current_session
from app.services import ledger_service
from app.services.billing import create_payment_order
from app.services.identity_service import WebSession

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

CAPABILITY_LABELS: dict[str, str] = {
    "reasoning_model": "Talos Reasoning",
    "fast_model": "Talos Fast",
    "code_model": "Talos Code",
    "vision_model": "Talos Vision",
    "web_search": "Web Search",
    "image_gen": "Image Generation",
    "browser_use": "Browser Use",
}


# ─── Response Models ──────────────────────────────────────────────────────────

class UserProfileResponse(BaseModel):
    account_id: str
    email: str
    role: str
    subscription_tier: str
    total_balance: int
    subscription_credits: int
    topup_credits: int


class DashboardBalanceResponse(BaseModel):
    subscription_credits: int
    topup_credits: int
    total: int


class CapabilityUsageSummary(BaseModel):
    capability_id: str
    label: str
    call_count: int
    credits_charged: int
    total_units: int


class DashboardUsageResponse(BaseModel):
    items: list[CapabilityUsageSummary]
    total_credits_charged: int


class TaskSummaryItem(BaseModel):
    task_id: str
    total_credits_spent: int
    call_count: int
    last_activity_at: str


class TaskListResponse(BaseModel):
    tasks: list[TaskSummaryItem]


class TaskCostEvent(BaseModel):
    event_id: str
    capability_id: str
    label: str
    credits_charged: int
    created_at: str


class TaskCostResponse(BaseModel):
    task_id: str
    total_credits_spent: int
    events: list[TaskCostEvent]


class BuyCreditsRequest(BaseModel):
    gateway: Literal["razorpay", "stripe"] = "razorpay"
    tier: str = "starter"  # starter, growth, scale


class BuyCreditsResponse(BaseModel):
    gateway: str
    order_id: Optional[str] = None
    checkout_url: Optional[str] = None
    amount: int
    currency: str
    credits: int


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserProfileResponse)
async def get_dashboard_me(
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns full profile for the authenticated session user.
    Account is derived exclusively from session.account_id.
    """
    result = await db.execute(select(Account).where(Account.account_id == session.account_id))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")

    breakdown = await ledger_service.get_balance_breakdown(db, session.account_id)
    sub_credits = breakdown.get("subscription_credits", 0) if "error" not in breakdown else 0
    topup_credits = breakdown.get("topup_credits", 0) if "error" not in breakdown else 0
    total_balance = sub_credits + topup_credits

    return UserProfileResponse(
        account_id=str(account.account_id),
        email=account.email,
        role=account.role,
        subscription_tier=account.subscription_tier,
        total_balance=total_balance,
        subscription_credits=sub_credits,
        topup_credits=topup_credits,
    )


@router.get("/balance", response_model=DashboardBalanceResponse)
async def get_dashboard_balance(
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns current balance breakdown (subscription vs top-up).
    Account derived exclusively from session.account_id.
    """
    breakdown = await ledger_service.get_balance_breakdown(db, session.account_id)
    if "error" in breakdown:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=breakdown["error"],
        )
    return DashboardBalanceResponse(
        subscription_credits=breakdown["subscription_credits"],
        topup_credits=breakdown["topup_credits"],
        total=breakdown["total_credits"],
    )


@router.get("/usage", response_model=DashboardUsageResponse)
async def get_dashboard_usage(
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns aggregated capability usage for the authenticated session.
    INVARIANT: Does not include the 'provider' field.
    """
    stmt = (
        select(
            PricingEvent.capability_id,
            func.count(PricingEvent.event_id).label("call_count"),
            func.coalesce(func.sum(PricingEvent.credits_charged), 0).label("credits_charged"),
            func.coalesce(func.sum(PricingEvent.actual_units), 0).label("total_units"),
        )
        .where(
            PricingEvent.account_id == session.account_id,
            PricingEvent.rejected.is_(False),
        )
        .group_by(PricingEvent.capability_id)
    )

    result = await db.execute(stmt)
    rows = result.all()

    items = []
    total_credits = 0
    for row in rows:
        cap_id = row.capability_id
        credits = int(row.credits_charged)
        total_credits += credits
        cap_id_str = str(cap_id or "")
        label_str = CAPABILITY_LABELS.get(cap_id_str) or (cap_id_str.replace("_", " ").title() if cap_id_str else "Unknown")
        items.append(
            CapabilityUsageSummary(
                capability_id=cap_id_str,
                label=label_str,
                call_count=int(row.call_count),
                credits_charged=credits,
                total_units=int(row.total_units),
            )
        )


    return DashboardUsageResponse(
        items=items,
        total_credits_charged=total_credits,
    )


@router.get("/tasks", response_model=TaskListResponse)
async def list_dashboard_tasks(
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
):
    """
    Returns task list with aggregated per-task credit spend for the session account.
    """
    stmt = (
        select(
            PricingEvent.task_id,
            func.coalesce(func.sum(PricingEvent.credits_charged), 0).label("total_credits"),
            func.count(PricingEvent.event_id).label("call_count"),
            func.max(PricingEvent.created_at).label("last_activity"),
        )
        .where(
            PricingEvent.account_id == session.account_id,
            PricingEvent.task_id.isnot(None),
            PricingEvent.rejected.is_(False),
        )
        .group_by(PricingEvent.task_id)
        .order_by(func.max(PricingEvent.created_at).desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.all()

    tasks = [
        TaskSummaryItem(
            task_id=row.task_id,
            total_credits_spent=int(row.total_credits),
            call_count=int(row.call_count),
            last_activity_at=row.last_activity.isoformat() if row.last_activity else "",
        )
        for row in rows
    ]
    return TaskListResponse(tasks=tasks)


@router.get("/tasks/{task_id}/cost", response_model=TaskCostResponse)
async def get_task_cost(
    task_id: str,
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns per-task cost and itemized breakdown for a given task_id belonging to the user.
    INVARIANT: Does not include the 'provider' field.
    """
    stmt = (
        select(PricingEvent)
        .where(
            PricingEvent.account_id == session.account_id,
            PricingEvent.task_id == task_id,
            PricingEvent.rejected.is_(False),
        )
        .order_by(PricingEvent.created_at.desc())
    )
    result = await db.execute(stmt)
    events = result.scalars().all()

    total_spent = sum(e.credits_charged for e in events)
    event_items = [
        TaskCostEvent(
            event_id=str(e.event_id),
            capability_id=e.capability_id,
            label=CAPABILITY_LABELS.get(e.capability_id) or e.capability_id.replace("_", " ").title(),
            credits_charged=e.credits_charged,
            created_at=e.created_at.isoformat(),
        )
        for e in events
    ]


    return TaskCostResponse(
        task_id=task_id,
        total_credits_spent=total_spent,
        events=event_items,
    )


@router.post("/buy-credits", response_model=BuyCreditsResponse)
async def buy_credits(
    req: BuyCreditsRequest,
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """
    Initiates a top-up credit purchase for the authenticated user via Razorpay or Stripe.
    Reuses existing unified create_payment_order service.
    """
    # Standard package catalog
    PACKAGES = {
        "starter": {"amount_inr": 49900, "amount_usd": 1000, "credits": 5000},
        "growth": {"amount_inr": 149900, "amount_usd": 2500, "credits": 20000},
        "scale": {"amount_inr": 499900, "amount_usd": 8000, "credits": 80000},
    }
    pkg = PACKAGES.get(req.tier, PACKAGES["starter"])

    if req.gateway == "razorpay":
        order = await create_payment_order(
            account_id=session.account_id,
            package_type="topup",
            credits=pkg["credits"],
            amount_minor=pkg["amount_inr"],
            currency="INR",
            gateway="razorpay",
        )
        return BuyCreditsResponse(
            gateway="razorpay",
            order_id=order.order_id,
            amount=order.amount_minor,
            currency=order.currency,
            credits=pkg["credits"],
        )
    else:
        order = await create_payment_order(
            account_id=session.account_id,
            package_type="topup",
            credits=pkg["credits"],
            amount_minor=pkg["amount_usd"],
            currency="USD",
            gateway="stripe",
        )
        return BuyCreditsResponse(
            gateway="stripe",
            checkout_url=order.checkout_url,
            amount=order.amount_minor,
            currency=order.currency,
            credits=pkg["credits"],
        )
# ─── GDPR Account Erasure ───────────────────────────────────────────────────

@router.delete("/account", status_code=status.HTTP_200_OK)
async def delete_user_account(
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """
    Permanently erases user account and all associated tokens, wallets, and usage data (GDPR Right to Erasure).
    """
    from sqlalchemy import delete
    from app.models.wallet import Wallet, CreditReservation
    from app.services.auth_service import DeviceToken
    from app.models.ledger import CreditTransaction
    from app.models.usage_event import UsageEvent
    from app.models.billing import BillingTransaction

    account_id = session.account_id

    # 1. Delete active device tokens
    await db.execute(delete(DeviceToken).where(DeviceToken.account_id == account_id))

    # 2. Delete wallet and its reservations
    w_res = await db.execute(select(Wallet).where(Wallet.account_id == account_id))
    wallet = w_res.scalar_one_or_none()
    if wallet:
        await db.execute(delete(CreditReservation).where(CreditReservation.wallet_id == wallet.wallet_id))
        await db.execute(delete(Wallet).where(Wallet.wallet_id == wallet.wallet_id))

    # 3. Delete user account record
    res = await db.execute(delete(Account).where(Account.account_id == account_id))
    await db.commit()

    if res.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found.",
        )

    return {
        "status": "success",
        "message": "Your account and all associated credentials have been permanently deleted.",
    }
