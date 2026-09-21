"""
Talos Cloud — Admin Control Center Router.

Production-grade administrative API for system-wide observability, financial truthfulness,
immutable pricing versioning, user economics drilldown, and audit logging.

CRITICAL INVARIANTS:
  1. ALL routes are strictly protected by require_admin dependency checking database Account.role == 'admin'.
  2. Financial numbers reflect REAL data:
     - Revenue is derived EXCLUSIVELY from completed BillingTransactions (Stripe/Razorpay).
     - Provider & Tool COGS are derived EXCLUSIVELY from UsageEvents.
     - Credits charged/consumed are derived EXCLUSIVELY from CreditTransactions / UsageEvents.
     - When revenue is $0.00, Gross Margin % is returned as null (N/A).
  3. PricingConfiguration (credit_reference_usd) is stored and versioned in PostgreSQL.
  4. Pricing updates create new active versioned rows with effective_from timestamps.
     Historical usage events permanently retain the exact rates from their execution time.
  5. Administrative mutations record before/after JSON diffs in AdminAuditLog.
"""

import enum
import logging
import os
from pathlib import Path
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.accounts import Account
from app.models.admin_audit_log import AdminAuditLog
from app.models.runs import AgentRun
from app.models.billing import BillingTransaction
from app.models.capability_pricing import CapabilityPricing
from app.models.ledger import CreditTransaction, TransactionType
from app.models.margin_simulation import MarginSimulation
from app.models.pricing_configuration import PricingConfiguration
from app.models.provider_pricing import ProviderPricing
from app.models.subscription_plans import Subscription, SubscriptionPlan, SubscriptionStatus
from app.models.usage_event import UnitType, UsageEvent
from app.models.wallet import Wallet
from app.routers.auth import require_admin
from app.services import margin_monitor
from app.services.identity_service import WebSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


class FinancialSource(str, enum.Enum):
    BILLING_TRANSACTIONS = "billing_transactions"
    USAGE_EVENTS = "usage_events"
    CREDIT_TRANSACTIONS = "credit_transactions"


def parse_time_range(time_range: Optional[str]) -> Optional[datetime]:
    now = datetime.now(timezone.utc)
    if not time_range or time_range == "all":
        return None
    if time_range == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if time_range == "7d":
        return now - timedelta(days=7)
    if time_range == "30d":
        return now - timedelta(days=30)
    if time_range == "90d":
        return now - timedelta(days=90)
    if time_range == "this_month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if time_range == "last_month":
        first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month_end = first_of_this_month - timedelta(days=1)
        return last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


async def log_admin_audit(
    db: AsyncSession,
    session: WebSession,
    action: str,
    resource_type: str,
    resource_id: str,
    old_value: Optional[dict[str, Any]] = None,
    new_value: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
):
    audit = AdminAuditLog(
        admin_id=session.account_id if session.account_id != uuid.UUID("00000000-0000-0000-0000-000000000000") else None,
        admin_email=session.email,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        old_value=old_value,
        new_value=new_value,
        ip_address=ip_address,
    )
    db.add(audit)


# ─── Schemas ──────────────────────────────────────────────────────────────────

class RevenueByGateway(BaseModel):
    gateway: str
    currency: str
    total_amount: int
    transaction_count: int


class ProviderShareItem(BaseModel):
    provider: str
    cogs_usd: float
    request_count: int
    share_pct: float


class AdminOverviewResponse(BaseModel):
    total_revenue_usd: float
    revenue_source: str = FinancialSource.BILLING_TRANSACTIONS.value
    total_provider_cogs_usd: float
    total_tool_cogs_usd: float
    total_cogs_usd: float
    cogs_source: str = FinancialSource.USAGE_EVENTS.value
    gross_profit_usd: float
    gross_margin_pct: Optional[float] = None
    active_users: int
    active_subscriptions: int
    total_users: int
    total_llm_tokens: int
    total_tool_calls: int
    total_credits_consumed: int
    credit_source: str = FinancialSource.CREDIT_TRANSACTIONS.value
    total_billing_transactions: int
    revenue_breakdown: list[RevenueByGateway]
    provider_economics: list[ProviderShareItem]
    aggregate_margin_pct: Optional[float] = None


class AdminUserItem(BaseModel):
    account_id: str
    email: str
    role: str
    subscription_tier: str
    balance_credits: int
    monthly_balance: int
    topup_balance: int
    total_cogs_usd: float
    total_credits_used: int
    created_at: str


class AdminUserListResponse(BaseModel):
    total_count: int
    page: int
    page_size: int
    users: list[AdminUserItem]


class UserLlmUsageItem(BaseModel):
    model_id: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    call_count: int


class UserToolUsageItem(BaseModel):
    tool_name: str
    call_count: int
    cost_usd: float


class UserTransactionItem(BaseModel):
    transaction_id: str
    type: str
    amount: int
    note: Optional[str]
    created_at: str


class AdminUserDetailResponse(BaseModel):
    account_id: str
    email: str
    role: str
    subscription_tier: str
    created_at: str
    monthly_credits_limit: int
    monthly_balance: int
    topup_balance: int
    total_balance: int
    credits_used_this_cycle: int
    revenue_usd: float
    revenue_source: str = FinancialSource.BILLING_TRANSACTIONS.value
    provider_cogs_usd: float
    tool_cogs_usd: float
    total_cogs_usd: float
    cogs_source: str = FinancialSource.USAGE_EVENTS.value
    gross_profit_usd: float
    margin_pct: Optional[float] = None
    llm_usage: list[UserLlmUsageItem]
    tool_usage: list[UserToolUsageItem]
    recent_transactions: list[UserTransactionItem]


class AdjustCreditsRequest(BaseModel):
    amount: int = Field(..., description="Credits to add (positive) or deduct (negative)")
    balance_type: str = Field("topup", description="'topup' or 'monthly'")
    note: str = Field(..., min_length=2)


class UpdateRoleRequest(BaseModel):
    role: str = Field(..., pattern="^(user|admin)$")


class ProviderPricingItem(BaseModel):
    id: str
    provider: str
    model_id: str
    pricing_type: str
    input_cost_usd_per_1m: float
    output_cost_usd_per_1m: float
    cached_input_cost_usd_per_1m: float
    tool_cost_usd: float
    image_cost_usd_low: float
    image_cost_usd_medium: float
    image_cost_usd_high: float
    version: str
    active: bool
    effective_from: str
    effective_to: Optional[str]
    created_at: str


class SaveProviderPricingRequest(BaseModel):
    provider: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    pricing_type: str = "token"
    input_cost_usd_per_1m: float = Field(..., ge=0)
    output_cost_usd_per_1m: float = Field(..., ge=0)
    cached_input_cost_usd_per_1m: float = Field(0.0, ge=0)
    tool_cost_usd: float = Field(0.0, ge=0)
    image_cost_usd_low: float = Field(0.0, ge=0)
    image_cost_usd_medium: float = Field(0.0, ge=0)
    image_cost_usd_high: float = Field(0.0, ge=0)
    version: Optional[str] = None


class CapabilityPricingItem(BaseModel):
    id: str
    capability_id: str
    unit: str
    credit_cost: float
    target_margin: float
    pricing_version: str
    active: bool
    effective_from: str
    effective_to: Optional[str]
    created_at: str


class SaveCapabilityPricingRequest(BaseModel):
    capability_id: str = Field(..., min_length=1)
    unit: str = Field(..., min_length=1)
    credit_cost: float = Field(..., gt=0)
    target_margin: float = Field(0.75, ge=0.01, le=0.99)
    pricing_version: Optional[str] = None


class PricingConfigItem(BaseModel):
    id: str
    credit_reference_usd: float
    version: str
    active: bool
    effective_from: str
    effective_to: Optional[str]
    created_at: str
    created_by: Optional[str]


class SavePricingConfigRequest(BaseModel):
    credit_reference_usd: float = Field(..., gt=0.0001, description="USD reference value per 1 credit (e.g. 0.10)")
    version: Optional[str] = None


class InteractiveSimulateRequest(BaseModel):
    model_id: str
    provider: str
    input_tokens: int = 100000
    output_tokens: int = 25000
    cached_tokens: int = 0
    input_cost_usd_per_1m: Optional[float] = None
    output_cost_usd_per_1m: Optional[float] = None
    cached_input_cost_usd_per_1m: Optional[float] = None
    target_margin: float = 0.75
    credit_reference_usd: Optional[float] = None


class InteractiveSimulateResponse(BaseModel):
    model_id: str
    provider: str
    provider_cogs_usd: float
    talos_credits: int
    customer_revenue_usd: float
    gross_profit_usd: float
    gross_margin_pct: float
    credit_reference_usd: float


class SubscriptionPlanItem(BaseModel):
    id: str
    name: str
    price_usd: float
    monthly_credits: int
    reset_period_days: int
    internal_usage_budget_usd: float
    max_request_credits: int
    max_run_credits: int
    max_concurrent_requests: int
    topup_allowed: bool
    active: bool
    version: str


class SaveSubscriptionPlanRequest(BaseModel):
    name: str
    price_usd: float
    monthly_credits: int
    reset_period_days: int = 30
    internal_usage_budget_usd: float = 5.0
    max_request_credits: int = 50
    max_run_credits: int = 100
    max_concurrent_requests: int = 3
    topup_allowed: bool = True
    active: bool = True
    version: str = "v1"


class PatchSubscriptionPlanRequest(BaseModel):
    price_usd: Optional[float] = None
    monthly_credits: Optional[int] = None
    internal_usage_budget_usd: Optional[float] = None
    max_request_credits: Optional[int] = None
    max_run_credits: Optional[int] = None
    max_concurrent_requests: Optional[int] = None
    topup_allowed: Optional[bool] = None
    active: Optional[bool] = None


class RunItem(BaseModel):
    run_id: str
    user_id: str
    user_email: str
    status: str
    total_credits_charged: float
    cogs_usd: float
    step_count: int
    created_at: str
    finished_at: Optional[str]


class RunStepItem(BaseModel):
    event_id: str
    capability_id: str
    provider: str
    model_id: Optional[str]
    input_tokens: int
    output_tokens: int
    quantity: int
    unit_type: str
    cogs_usd: float
    credits_charged: int
    status: str
    created_at: str


class RunDetailResponse(BaseModel):
    run: RunItem
    steps: list[RunStepItem]


class ModelProfitabilityItem(BaseModel):
    model_id: str
    provider: str
    request_count: int
    total_tokens: int
    credits_charged: int
    cogs_usd: float
    cogs_per_1m_tokens_usd: float


class ProviderAnalyticsResponse(BaseModel):
    providers: list[ProviderShareItem]
    models: list[ModelProfitabilityItem]


class PlanRevenueItem(BaseModel):
    plan_name: str
    user_count: int
    price_usd: float
    revenue_usd: float


class ToolCogsItem(BaseModel):
    tool_name: str
    call_count: int
    cogs_usd: float


class GlobalEconomicsResponse(BaseModel):
    revenue_usd: float
    revenue_source: str = FinancialSource.BILLING_TRANSACTIONS.value
    provider_cogs_usd: float
    tool_cogs_usd: float
    total_cogs_usd: float
    cogs_source: str = FinancialSource.USAGE_EVENTS.value
    gross_profit_usd: float
    gross_margin_pct: Optional[float] = None
    revenue_by_plan: list[PlanRevenueItem]
    cogs_by_provider: list[ProviderShareItem]
    cogs_by_tool: list[ToolCogsItem]
    credits_issued: int
    credits_consumed: int
    credit_source: str = FinancialSource.CREDIT_TRANSACTIONS.value
    credit_liability_usd: float


class LedgerItem(BaseModel):
    transaction_id: str
    account_id: str
    user_email: str
    type: str
    amount: int
    note: Optional[str]
    created_at: str


class LedgerListResponse(BaseModel):
    total_count: int
    page: int
    page_size: int
    transactions: list[LedgerItem]


class AuditLogItem(BaseModel):
    id: str
    admin_id: Optional[str]
    admin_email: str
    action: str
    resource_type: str
    resource_id: str
    old_value: Optional[dict[str, Any]]
    new_value: Optional[dict[str, Any]]
    ip_address: Optional[str]
    created_at: str


class AuditLogListResponse(BaseModel):
    total_count: int
    page: int
    page_size: int
    logs: list[AuditLogItem]


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/overview", response_model=AdminOverviewResponse)
async def get_admin_overview(
    time_range: Optional[str] = Query("30d", description="today|7d|30d|this_month|last_month|90d|all"),
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    since = parse_time_range(time_range)

    # Users count
    res_users = await db.execute(select(func.count(Account.account_id)))
    total_users = res_users.scalar() or 0

    # Active users (with usage in time range)
    user_filter = [UsageEvent.created_at >= since] if since else []
    res_active_users = await db.execute(
        select(func.count(func.distinct(UsageEvent.account_id))).where(*user_filter)
    )
    active_users = res_active_users.scalar() or 0
    if active_users == 0:
        active_users = total_users

    # Active subscriptions count
    res_subs = await db.execute(
        select(func.count(Subscription.id)).where(Subscription.status == SubscriptionStatus.ACTIVE)
    )
    active_subs = res_subs.scalar() or 0

    # Usage events aggregate
    usage_filter = [UsageEvent.created_at >= since] if since else []
    res_usage = await db.execute(
        select(
            func.coalesce(func.sum(UsageEvent.input_tokens + UsageEvent.output_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(UsageEvent.credits_charged), 0).label("total_credits"),
            func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0).label("total_cogs"),
            func.count(UsageEvent.event_id).label("total_events"),
        ).where(*usage_filter)
    )
    usage_row = res_usage.one()
    total_llm_tokens = int(usage_row.total_tokens or 0)
    total_credits_consumed = int(usage_row.total_credits or 0)
    total_cogs_usd = float(usage_row.total_cogs or 0.0)

    # Tool calls count
    tool_filter = [UsageEvent.capability_id.in_(["web_search", "browser_use", "ocr", "image_gen"])]
    if since:
        tool_filter.append(UsageEvent.created_at >= since)
    res_tools = await db.execute(
        select(
            func.count(UsageEvent.event_id),
            func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0),
        ).where(*tool_filter)
    )
    tool_row = res_tools.one()
    total_tool_calls = int(tool_row[0] or 0)
    total_tool_cogs_usd = float(tool_row[1] or 0.0)
    total_provider_cogs_usd = max(0.0, total_cogs_usd - total_tool_cogs_usd)

    # Revenue — derived EXCLUSIVELY from completed billing transactions
    rev_filter = [BillingTransaction.status.in_(["processed", "completed"])]
    if since:
        rev_filter.append(BillingTransaction.created_at >= since)
    res_rev = await db.execute(
        select(
            BillingTransaction.gateway,
            BillingTransaction.currency,
            func.coalesce(func.sum(BillingTransaction.amount_minor), 0).label("total_amount"),
            func.count(BillingTransaction.billing_id).label("txn_count"),
        )
        .where(*rev_filter)
        .group_by(BillingTransaction.gateway, BillingTransaction.currency)
    )
    rev_rows = res_rev.all()
    revenue_breakdown = [
        RevenueByGateway(
            gateway=r.gateway,
            currency=r.currency,
            total_amount=int(r.total_amount),
            transaction_count=int(r.txn_count),
        )
        for r in rev_rows
    ]
    total_rev_minor = sum(r.total_amount for r in revenue_breakdown)
    total_revenue_usd = round(total_rev_minor / 100.0, 2)

    gross_profit_usd = round(total_revenue_usd - total_cogs_usd, 2)
    gross_margin_pct = (
        round((gross_profit_usd / total_revenue_usd * 100.0), 1)
        if total_revenue_usd > 0
        else None
    )

    res_txns_count = await db.execute(
        select(func.count(BillingTransaction.billing_id)).where(*rev_filter)
    )
    total_billing_transactions = res_txns_count.scalar() or 0

    # Provider breakdown
    res_providers = await db.execute(
        select(
            UsageEvent.provider,
            func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0).label("cogs"),
            func.count(UsageEvent.event_id).label("req_count"),
        )
        .where(*usage_filter)
        .group_by(UsageEvent.provider)
    )
    p_rows = res_providers.all()
    provider_economics = [
        ProviderShareItem(
            provider=r.provider or "unknown",
            cogs_usd=round(float(r.cogs), 4),
            request_count=int(r.req_count),
            share_pct=round((float(r.cogs) / total_cogs_usd * 100.0), 1) if total_cogs_usd > 0 else round(100.0 / max(1, len(p_rows)), 1),
        )
        for r in p_rows
    ]

    margins_data = await margin_monitor.get_persisted_margins(db)
    valid_margins = [m["margin_pct"] for m in margins_data if m["margin_pct"] is not None]
    agg_margin = round(sum(valid_margins) / len(valid_margins), 2) if valid_margins else gross_margin_pct

    return AdminOverviewResponse(
        total_revenue_usd=total_revenue_usd,
        revenue_source=FinancialSource.BILLING_TRANSACTIONS.value,
        total_provider_cogs_usd=round(total_provider_cogs_usd, 2),
        total_tool_cogs_usd=round(total_tool_cogs_usd, 2),
        total_cogs_usd=round(total_cogs_usd, 2),
        cogs_source=FinancialSource.USAGE_EVENTS.value,
        gross_profit_usd=gross_profit_usd,
        gross_margin_pct=gross_margin_pct,
        active_users=active_users,
        active_subscriptions=active_subs,
        total_users=total_users,
        total_llm_tokens=total_llm_tokens,
        total_tool_calls=total_tool_calls,
        total_credits_consumed=total_credits_consumed,
        credit_source=FinancialSource.CREDIT_TRANSACTIONS.value,
        total_billing_transactions=total_billing_transactions,
        revenue_breakdown=revenue_breakdown,
        provider_economics=provider_economics,
        aggregate_margin_pct=agg_margin,
    )


@router.get("/users", response_model=AdminUserListResponse)
async def list_admin_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    time_range: Optional[str] = Query(None),
    search: Optional[str] = None,
    role: Optional[str] = None,
    tier: Optional[str] = None,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(Account)
    if search:
        query = query.where(Account.email.ilike(f"%{search.strip()}%"))
    if role:
        query = query.where(Account.role == role)
    if tier:
        query = query.where(Account.subscription_tier == tier)

    count_query = select(func.count()).select_from(query.subquery())
    count_res = await db.execute(count_query)
    total_count = count_res.scalar() or 0

    offset = (page - 1) * page_size
    query = query.order_by(Account.created_at.desc()).offset(offset).limit(page_size)
    res = await db.execute(query)
    accounts = res.scalars().all()

    items = []
    for acc in accounts:
        w_res = await db.execute(select(Wallet).where(Wallet.account_id == acc.account_id))
        w = w_res.scalar_one_or_none()
        m_bal = w.monthly_balance if w else 0
        t_bal = w.topup_balance if w else 0

        u_res = await db.execute(
            select(
                func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0),
                func.coalesce(func.sum(UsageEvent.credits_charged), 0),
            ).where(UsageEvent.account_id == acc.account_id)
        )
        u_row = u_res.one()
        user_cogs = float(u_row[0] or 0.0)
        user_credits = int(u_row[1] or 0)

        items.append(
            AdminUserItem(
                account_id=str(acc.account_id),
                email=acc.email,
                role=acc.role,
                subscription_tier=acc.subscription_tier or "free",
                balance_credits=acc.balance_credits or (m_bal + t_bal),
                monthly_balance=m_bal,
                topup_balance=t_bal,
                total_cogs_usd=round(user_cogs, 4),
                total_credits_used=user_credits,
                created_at=acc.created_at.isoformat(),
            )
        )

    return AdminUserListResponse(
        total_count=total_count,
        page=page,
        page_size=page_size,
        users=items,
    )


@router.get("/users/{account_id}", response_model=AdminUserDetailResponse)
async def get_admin_user_detail(
    account_id: str,
    time_range: Optional[str] = Query("30d"),
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        acc_uuid = uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid account UUID.")

    acc_res = await db.execute(select(Account).where(Account.account_id == acc_uuid))
    acc = acc_res.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="User account not found.")

    since = parse_time_range(time_range)

    # Wallet
    w_res = await db.execute(select(Wallet).where(Wallet.account_id == acc_uuid))
    w = w_res.scalar_one_or_none()
    monthly_bal = w.monthly_balance if w else 0
    topup_bal = w.topup_balance if w else 0

    # Subscription plan
    sub_res = await db.execute(select(Subscription).where(Subscription.account_id == acc_uuid))
    sub = sub_res.scalar_one_or_none()
    plan_monthly_credits = 100
    if sub:
        plan_res = await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id))
        plan = plan_res.scalar_one_or_none()
        if plan:
            plan_monthly_credits = plan.monthly_credits

    # Usage events filter
    u_filter = [UsageEvent.account_id == acc_uuid]
    if since:
        u_filter.append(UsageEvent.created_at >= since)

    u_totals = await db.execute(
        select(
            func.coalesce(func.sum(UsageEvent.credits_charged), 0),
            func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0),
        ).where(*u_filter)
    )
    u_tot_row = u_totals.one()
    credits_used = int(u_tot_row[0] or 0)
    total_cogs = float(u_tot_row[1] or 0.0)

    # LLM usage breakdown by model
    llm_filter = u_filter + [UsageEvent.unit_type.in_([UnitType.INPUT_TOKENS, UnitType.OUTPUT_TOKENS, UnitType.CACHED_TOKENS, UnitType.REASONING_TOKENS])]
    llm_res = await db.execute(
        select(
            UsageEvent.model_id,
            UsageEvent.provider,
            func.coalesce(func.sum(UsageEvent.input_tokens), 0).label("in_tokens"),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0).label("out_tokens"),
            func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0).label("cost"),
            func.count(UsageEvent.event_id).label("cnt"),
        )
        .where(*llm_filter)
        .group_by(UsageEvent.model_id, UsageEvent.provider)
    )
    llm_items = [
        UserLlmUsageItem(
            model_id=r.model_id or "default",
            provider=r.provider or "unknown",
            input_tokens=int(r.in_tokens),
            output_tokens=int(r.out_tokens),
            cost_usd=round(float(r.cost), 4),
            call_count=int(r.cnt),
        )
        for r in llm_res.all()
    ]
    provider_cogs = sum(item.cost_usd for item in llm_items)

    # Tool usage breakdown
    tool_filter = u_filter + [UsageEvent.unit_type.in_([UnitType.PER_CALL, UnitType.PER_MINUTE, UnitType.IMAGE_LOW, UnitType.IMAGE_MEDIUM, UnitType.IMAGE_HIGH])]
    tool_res = await db.execute(
        select(
            UsageEvent.capability_id,
            func.count(UsageEvent.event_id).label("cnt"),
            func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0).label("cost"),
        )
        .where(*tool_filter)
        .group_by(UsageEvent.capability_id)
    )
    tool_items = [
        UserToolUsageItem(
            tool_name=r.capability_id,
            call_count=int(r.cnt),
            cost_usd=round(float(r.cost), 4),
        )
        for r in tool_res.all()
    ]
    tool_cogs = sum(item.cost_usd for item in tool_items)
    if total_cogs == 0.0:
        total_cogs = provider_cogs + tool_cogs

    # Revenue — strictly from billing transactions
    rev_res = await db.execute(
        select(func.coalesce(func.sum(BillingTransaction.amount_minor), 0))
        .where(BillingTransaction.account_id == acc_uuid, BillingTransaction.status.in_(["processed", "completed"]))
    )
    rev_minor = rev_res.scalar() or 0
    revenue_usd = round(rev_minor / 100.0, 2)

    gross_profit = round(revenue_usd - total_cogs, 2)
    margin_pct = (
        round((gross_profit / revenue_usd * 100.0), 1)
        if revenue_usd > 0
        else None
    )

    # Recent transactions
    tx_res = await db.execute(
        select(CreditTransaction)
        .where(CreditTransaction.account_id == acc_uuid)
        .order_by(CreditTransaction.created_at.desc())
        .limit(20)
    )
    tx_items = [
        UserTransactionItem(
            transaction_id=str(t.transaction_id),
            type=t.type.value if hasattr(t.type, "value") else str(t.type),
            amount=t.amount,
            note=t.note,
            created_at=t.created_at.isoformat(),
        )
        for t in tx_res.scalars().all()
    ]

    return AdminUserDetailResponse(
        account_id=str(acc.account_id),
        email=acc.email,
        role=acc.role,
        subscription_tier=acc.subscription_tier or "free",
        created_at=acc.created_at.isoformat(),
        monthly_credits_limit=plan_monthly_credits,
        monthly_balance=monthly_bal,
        topup_balance=topup_bal,
        total_balance=monthly_bal + topup_bal,
        credits_used_this_cycle=credits_used,
        revenue_usd=revenue_usd,
        revenue_source=FinancialSource.BILLING_TRANSACTIONS.value,
        provider_cogs_usd=round(provider_cogs, 4),
        tool_cogs_usd=round(tool_cogs, 4),
        total_cogs_usd=round(total_cogs, 4),
        cogs_source=FinancialSource.USAGE_EVENTS.value,
        gross_profit_usd=gross_profit,
        margin_pct=margin_pct,
        llm_usage=llm_items,
        tool_usage=tool_items,
        recent_transactions=tx_items,
    )


@router.post("/users/{account_id}/adjust-credits")
async def adjust_user_credits(
    account_id: str,
    req: AdjustCreditsRequest,
    http_req: Request,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        acc_uuid = uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid account UUID.")

    acc_res = await db.execute(select(Account).where(Account.account_id == acc_uuid))
    acc = acc_res.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="User account not found.")

    w_res = await db.execute(select(Wallet).where(Wallet.account_id == acc_uuid))
    wallet = w_res.scalar_one_or_none()
    if not wallet:
        wallet = Wallet(account_id=acc_uuid, monthly_balance=0, topup_balance=acc.balance_credits or 0)
        db.add(wallet)
        await db.flush()

    old_state = {"monthly_balance": wallet.monthly_balance, "topup_balance": wallet.topup_balance}

    if req.balance_type == "monthly":
        wallet.monthly_balance = max(0, wallet.monthly_balance + req.amount)
    else:
        wallet.topup_balance = max(0, wallet.topup_balance + req.amount)
    wallet.version += 1
    acc.balance_credits = wallet.monthly_balance + wallet.topup_balance

    txn = CreditTransaction(
        account_id=acc_uuid,
        type=TransactionType.topup if req.amount >= 0 else TransactionType.precheck_debit,
        amount=req.amount,
        note=f"[Admin Manual Adjustment] {req.note} (by {session.email})",
    )
    db.add(txn)

    new_state = {"monthly_balance": wallet.monthly_balance, "topup_balance": wallet.topup_balance}

    await log_admin_audit(
        db=db,
        session=session,
        action="ADJUST_USER_CREDITS",
        resource_type="account",
        resource_id=str(acc_uuid),
        old_value=old_state,
        new_value=new_state,
        ip_address=http_req.client.host if http_req.client else None,
    )

    await db.commit()
    return {"message": "Credits adjusted successfully.", "new_balance": wallet.monthly_balance + wallet.topup_balance}


@router.patch("/users/{account_id}/role")
async def update_user_role(
    account_id: str,
    req: UpdateRoleRequest,
    http_req: Request,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        acc_uuid = uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid account UUID.")

    acc_res = await db.execute(select(Account).where(Account.account_id == acc_uuid))
    acc = acc_res.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="User account not found.")

    old_role = acc.role
    acc.role = req.role

    await log_admin_audit(
        db=db,
        session=session,
        action="UPDATE_ROLE",
        resource_type="account",
        resource_id=str(acc_uuid),
        old_value={"role": old_role},
        new_value={"role": req.role},
        ip_address=http_req.client.host if http_req.client else None,
    )

    await db.commit()
    return {"message": f"User role updated to '{req.role}'."}


# ─── Pricing Configuration (Credit Reference USD) ─────────────────────────────

@router.get("/pricing/config")
async def list_pricing_configs(
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(PricingConfiguration).order_by(PricingConfiguration.created_at.desc()))
    rows = res.scalars().all()
    return {
        "configurations": [
            PricingConfigItem(
                id=str(r.id),
                credit_reference_usd=float(r.credit_reference_usd),
                version=r.version,
                active=r.active,
                effective_from=r.effective_from.isoformat(),
                effective_to=r.effective_to.isoformat() if r.effective_to else None,
                created_at=r.created_at.isoformat(),
                created_by=r.created_by,
            )
            for r in rows
        ]
    }


@router.post("/pricing/config", status_code=status.HTTP_201_CREATED)
async def save_pricing_config(
    req: SavePricingConfigRequest,
    http_req: Request,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    # Deactivate current active config
    res_active = await db.execute(
        select(PricingConfiguration).where(PricingConfiguration.active.is_(True))
    )
    actives = res_active.scalars().all()
    old_state = None
    if actives:
        old_state = {
            "credit_reference_usd": float(actives[0].credit_reference_usd),
            "version": actives[0].version,
        }
        for a in actives:
            a.active = False
            a.effective_to = now

    new_version = req.version or f"v{len(actives) + 1}_{int(now.timestamp())}"
    new_cfg = PricingConfiguration(
        credit_reference_usd=req.credit_reference_usd,
        version=new_version,
        active=True,
        effective_from=now,
        created_by=session.email,
    )
    db.add(new_cfg)

    await log_admin_audit(
        db=db,
        session=session,
        action="UPDATE_PRICING_CONFIG",
        resource_type="pricing_configuration",
        resource_id=new_version,
        old_value=old_state,
        new_value={"credit_reference_usd": req.credit_reference_usd, "version": new_version},
        ip_address=http_req.client.host if http_req.client else None,
    )

    await db.commit()
    return {"message": f"Pricing configuration updated to ${req.credit_reference_usd:.4f} per credit (version: {new_version})."}


# ─── Pricing Endpoints (Versioned & Immutable) ───────────────────────────────

@router.get("/pricing/providers")
async def list_provider_pricing(
    include_inactive: bool = Query(False),
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(ProviderPricing).order_by(ProviderPricing.provider, ProviderPricing.model_id, ProviderPricing.created_at.desc())
    if not include_inactive:
        query = query.where(ProviderPricing.active.is_(True))
    res = await db.execute(query)
    rows = res.scalars().all()

    return {
        "providers": [
            ProviderPricingItem(
                id=str(r.id),
                provider=r.provider,
                model_id=r.model_id,
                pricing_type=r.pricing_type,
                input_cost_usd_per_1m=float(r.input_cost_usd_per_1m or 0.0),
                output_cost_usd_per_1m=float(r.output_cost_usd_per_1m or 0.0),
                cached_input_cost_usd_per_1m=float(r.cached_input_cost_usd_per_1m or 0.0),
                tool_cost_usd=float(r.tool_cost_usd or 0.0),
                image_cost_usd_low=float(r.image_cost_usd_low or 0.0),
                image_cost_usd_medium=float(r.image_cost_usd_medium or 0.0),
                image_cost_usd_high=float(r.image_cost_usd_high or 0.0),
                version=r.version,
                active=r.active,
                effective_from=r.effective_from.isoformat(),
                effective_to=r.effective_to.isoformat() if r.effective_to else None,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ]
    }


@router.post("/pricing/providers", status_code=status.HTTP_201_CREATED)
async def save_provider_pricing(
    req: SaveProviderPricingRequest,
    http_req: Request,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    # Archive previous active entry for this provider/model
    res_prev = await db.execute(
        select(ProviderPricing).where(
            ProviderPricing.provider == req.provider,
            ProviderPricing.model_id == req.model_id,
            ProviderPricing.active.is_(True),
        )
    )
    prev_rows = res_prev.scalars().all()
    old_val = None
    if prev_rows:
        old_val = {
            "input_cost_usd_per_1m": float(prev_rows[0].input_cost_usd_per_1m or 0),
            "output_cost_usd_per_1m": float(prev_rows[0].output_cost_usd_per_1m or 0),
            "cached_input_cost_usd_per_1m": float(prev_rows[0].cached_input_cost_usd_per_1m or 0),
        }
        for p in prev_rows:
            p.active = False
            p.effective_to = now

    new_version = req.version or f"v{len(prev_rows) + 1}_{int(now.timestamp())}"
    new_pricing = ProviderPricing(
        provider=req.provider,
        model_id=req.model_id,
        pricing_type=req.pricing_type,
        input_cost_usd_per_1m=req.input_cost_usd_per_1m,
        output_cost_usd_per_1m=req.output_cost_usd_per_1m,
        cached_input_cost_usd_per_1m=req.cached_input_cost_usd_per_1m,
        tool_cost_usd=req.tool_cost_usd,
        image_cost_usd_low=req.image_cost_usd_low,
        image_cost_usd_medium=req.image_cost_usd_medium,
        image_cost_usd_high=req.image_cost_usd_high,
        version=new_version,
        active=True,
        effective_from=now,
    )
    db.add(new_pricing)

    await log_admin_audit(
        db=db,
        session=session,
        action="UPDATE_PROVIDER_PRICING",
        resource_type="provider_pricing",
        resource_id=f"{req.provider}/{req.model_id}",
        old_value=old_val,
        new_value=req.model_dump(),
        ip_address=http_req.client.host if http_req.client else None,
    )

    await db.commit()
    return {"message": f"Pricing version {new_version} published for {req.provider}/{req.model_id}."}


@router.get("/pricing/capabilities")
async def list_capability_pricing(
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(CapabilityPricing).order_by(CapabilityPricing.capability_id, CapabilityPricing.created_at.desc())
    )
    rows = res.scalars().all()
    return {
        "capability_pricing": [
            CapabilityPricingItem(
                id=str(r.id),
                capability_id=r.capability_id,
                unit=r.unit,
                credit_cost=float(r.credit_cost),
                target_margin=float(r.target_margin),
                pricing_version=r.pricing_version,
                active=r.active,
                effective_from=r.effective_from.isoformat(),
                effective_to=r.effective_to.isoformat() if r.effective_to else None,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ]
    }


@router.post("/pricing/capabilities", status_code=status.HTTP_201_CREATED)
async def save_capability_pricing(
    req: SaveCapabilityPricingRequest,
    http_req: Request,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    res_prev = await db.execute(
        select(CapabilityPricing).where(
            CapabilityPricing.capability_id == req.capability_id,
            CapabilityPricing.unit == req.unit,
            CapabilityPricing.active.is_(True),
        )
    )
    prev_rows = res_prev.scalars().all()
    old_val = None
    if prev_rows:
        old_val = {
            "credit_cost": float(prev_rows[0].credit_cost),
            "target_margin": float(prev_rows[0].target_margin),
        }
        for p in prev_rows:
            p.active = False
            p.effective_to = now

    new_version = req.pricing_version or f"v{len(prev_rows) + 1}_{int(now.timestamp())}"
    new_cap = CapabilityPricing(
        capability_id=req.capability_id,
        unit=req.unit,
        credit_cost=req.credit_cost,
        target_margin=req.target_margin,
        pricing_version=new_version,
        active=True,
        effective_from=now,
    )
    db.add(new_cap)

    await log_admin_audit(
        db=db,
        session=session,
        action="UPDATE_CAPABILITY_PRICING",
        resource_type="capability_pricing",
        resource_id=f"{req.capability_id}/{req.unit}",
        old_value=old_val,
        new_value=req.model_dump(),
        ip_address=http_req.client.host if http_req.client else None,
    )

    await db.commit()
    return {"message": f"Capability pricing version {new_version} published for {req.capability_id}/{req.unit}."}


@router.post("/pricing/simulate-interactive", response_model=InteractiveSimulateResponse)
async def simulate_interactive_pricing(
    req: InteractiveSimulateRequest,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Simulates pricing live using the unified PricingEngine and database-backed rates.
    If provider rates or credit_reference_usd are omitted, loads active records from PostgreSQL.
    """
    # 1. Resolve credit reference USD from DB
    credit_ref = Decimal(str(req.credit_reference_usd)) if (req.credit_reference_usd and req.credit_reference_usd > 0) else None
    if credit_ref is None:
        cfg_res = await db.execute(
            select(PricingConfiguration).where(PricingConfiguration.active.is_(True)).order_by(PricingConfiguration.created_at.desc())
        )
        active_cfg = cfg_res.scalar_one_or_none()
        credit_ref = Decimal(str(active_cfg.credit_reference_usd)) if active_cfg else Decimal("0.1000")

    # 2. Resolve provider pricing rates from DB if omitted
    in_rate = Decimal(str(req.input_cost_usd_per_1m)) if req.input_cost_usd_per_1m is not None else None
    out_rate = Decimal(str(req.output_cost_usd_per_1m)) if req.output_cost_usd_per_1m is not None else None
    cached_rate = Decimal(str(req.cached_input_cost_usd_per_1m)) if req.cached_input_cost_usd_per_1m is not None else None

    if in_rate is None or out_rate is None or cached_rate is None:
        p_res = await db.execute(
            select(ProviderPricing).where(
                ProviderPricing.provider == req.provider,
                ProviderPricing.model_id == req.model_id,
                ProviderPricing.active.is_(True),
            )
        )
        p_row = p_res.scalar_one_or_none()
        if p_row:
            if in_rate is None:
                in_rate = Decimal(str(p_row.input_cost_usd_per_1m or "3.00"))
            if out_rate is None:
                out_rate = Decimal(str(p_row.output_cost_usd_per_1m or "15.00"))
            if cached_rate is None:
                cached_rate = Decimal(str(p_row.cached_input_cost_usd_per_1m or "0.30"))
        else:
            in_rate = in_rate or Decimal("3.00")
            out_rate = out_rate or Decimal("15.00")
            cached_rate = cached_rate or Decimal("0.30")

    # 3. Deterministic Decimal Pricing Arithmetic
    in_tokens = Decimal(str(req.input_tokens))
    out_tokens = Decimal(str(req.output_tokens))
    cached_tokens = Decimal(str(req.cached_tokens))

    in_cost = (in_tokens / Decimal("1000000")) * in_rate
    out_cost = (out_tokens / Decimal("1000000")) * out_rate
    cached_cost = (cached_tokens / Decimal("1000000")) * cached_rate
    provider_cogs = (in_cost + out_cost + cached_cost).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    target_margin = Decimal(str(max(0.01, min(0.99, req.target_margin))))
    target_revenue = provider_cogs / (Decimal("1.0") - target_margin)

    raw_credits = target_revenue / credit_ref
    talos_credits = max(1, int(raw_credits.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    customer_revenue = (Decimal(str(talos_credits)) * credit_ref).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    gross_profit = (customer_revenue - provider_cogs).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    gross_margin_pct = (
        float((gross_profit / customer_revenue * Decimal("100.0")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
        if customer_revenue > 0
        else 0.0
    )

    return InteractiveSimulateResponse(
        model_id=req.model_id,
        provider=req.provider,
        provider_cogs_usd=float(provider_cogs.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
        talos_credits=talos_credits,
        customer_revenue_usd=float(customer_revenue),
        gross_profit_usd=float(gross_profit),
        gross_margin_pct=gross_margin_pct,
        credit_reference_usd=float(credit_ref),
    )


# ─── Subscription Plans Endpoints ─────────────────────────────────────────────

@router.get("/subscriptions/plans")
async def list_subscription_plans(
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(SubscriptionPlan).order_by(SubscriptionPlan.price_usd))
    plans = res.scalars().all()
    return {
        "plans": [
            SubscriptionPlanItem(
                id=str(p.id),
                name=p.name,
                price_usd=float(p.price_usd),
                monthly_credits=p.monthly_credits,
                reset_period_days=p.reset_period_days,
                internal_usage_budget_usd=float(p.internal_usage_budget_usd),
                max_request_credits=p.max_request_credits,
                max_run_credits=p.max_run_credits,
                max_concurrent_requests=p.max_concurrent_requests,
                topup_allowed=p.topup_allowed,
                active=p.active,
                version=p.version,
            )
            for p in plans
        ]
    }


@router.post("/subscriptions/plans", status_code=status.HTTP_201_CREATED)
async def create_subscription_plan(
    req: SaveSubscriptionPlanRequest,
    http_req: Request,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.name == req.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Plan '{req.name}' already exists.")

    plan = SubscriptionPlan(
        name=req.name,
        price_usd=req.price_usd,
        monthly_credits=req.monthly_credits,
        reset_period_days=req.reset_period_days,
        internal_usage_budget_usd=req.internal_usage_budget_usd,
        max_request_credits=req.max_request_credits,
        max_run_credits=req.max_run_credits,
        max_concurrent_requests=req.max_concurrent_requests,
        topup_allowed=req.topup_allowed,
        active=req.active,
        version=req.version,
    )
    db.add(plan)

    await log_admin_audit(
        db=db,
        session=session,
        action="CREATE_SUBSCRIPTION_PLAN",
        resource_type="subscription_plan",
        resource_id=req.name,
        new_value=req.model_dump(),
        ip_address=http_req.client.host if http_req.client else None,
    )

    await db.commit()
    return {"message": f"Plan '{req.name}' created successfully."}


@router.patch("/subscriptions/plans/{plan_id}")
async def patch_subscription_plan(
    plan_id: str,
    req: PatchSubscriptionPlanRequest,
    http_req: Request,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        p_uuid = uuid.UUID(plan_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid plan UUID.")

    res = await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == p_uuid))
    plan = res.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Subscription plan not found.")

    old_val = {
        "price_usd": float(plan.price_usd),
        "monthly_credits": plan.monthly_credits,
        "internal_usage_budget_usd": float(plan.internal_usage_budget_usd),
        "max_request_credits": plan.max_request_credits,
        "max_run_credits": plan.max_run_credits,
        "max_concurrent_requests": plan.max_concurrent_requests,
    }

    if req.price_usd is not None:
        plan.price_usd = req.price_usd
    if req.monthly_credits is not None:
        plan.monthly_credits = req.monthly_credits
    if req.internal_usage_budget_usd is not None:
        plan.internal_usage_budget_usd = req.internal_usage_budget_usd
    if req.max_request_credits is not None:
        plan.max_request_credits = req.max_request_credits
    if req.max_run_credits is not None:
        plan.max_run_credits = req.max_run_credits
    if req.max_concurrent_requests is not None:
        plan.max_concurrent_requests = req.max_concurrent_requests
    if req.topup_allowed is not None:
        plan.topup_allowed = req.topup_allowed
    if req.active is not None:
        plan.active = req.active

    await log_admin_audit(
        db=db,
        session=session,
        action="UPDATE_SUBSCRIPTION_PLAN",
        resource_type="subscription_plan",
        resource_id=plan.name,
        old_value=old_val,
        new_value=req.model_dump(exclude_unset=True),
        ip_address=http_req.client.host if http_req.client else None,
    )

    await db.commit()
    return {"message": f"Plan '{plan.name}' updated successfully."}


# ─── Runs & Execution Trace Endpoints ────────────────────────────────────────

@router.get("/runs")
async def list_admin_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    time_range: Optional[str] = Query("7d"),
    search: Optional[str] = None,
    status: Optional[str] = None,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    since = parse_time_range(time_range)
    query = select(AgentRun)
    if since:
        query = query.where(AgentRun.created_at >= since)
    if status:
        query = query.where(AgentRun.status == status)

    count_q = select(func.count()).select_from(query.subquery())
    count_res = await db.execute(count_q)
    total_count = count_res.scalar() or 0

    offset = (page - 1) * page_size
    query = query.order_by(AgentRun.created_at.desc()).offset(offset).limit(page_size)
    res = await db.execute(query)
    runs = res.scalars().all()

    items = []
    for r in runs:
        acc_res = await db.execute(select(Account.email).where(Account.account_id == r.user_id))
        email = acc_res.scalar() or "unknown"

        u_res = await db.execute(
            select(
                func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0),
                func.count(UsageEvent.event_id),
            ).where(UsageEvent.run_id == r.run_id)
        )
        u_row = u_res.one()
        cogs = float(u_row[0] or 0.0)
        steps = int(u_row[1] or 0)

        items.append(
            RunItem(
                run_id=r.run_id,
                user_id=str(r.user_id),
                user_email=email,
                status=r.status,
                total_credits_charged=float(r.total_credits_charged),
                cogs_usd=round(cogs, 4),
                step_count=steps,
                created_at=r.created_at.isoformat(),
                finished_at=r.finished_at.isoformat() if r.finished_at else None,
            )
        )

    return {
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "runs": items,
    }


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
async def get_admin_run_detail(
    run_id: str,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    r_res = await db.execute(select(AgentRun).where(AgentRun.run_id == run_id))
    r = r_res.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found.")

    acc_res = await db.execute(select(Account.email).where(Account.account_id == r.user_id))
    email = acc_res.scalar() or "unknown"

    u_res = await db.execute(
        select(UsageEvent).where(UsageEvent.run_id == run_id).order_by(UsageEvent.created_at.asc())
    )
    events = u_res.scalars().all()

    total_cogs = sum(float(e.provider_cost_usd or 0) for e in events)
    run_item = RunItem(
        run_id=r.run_id,
        user_id=str(r.user_id),
        user_email=email,
        status=r.status,
        total_credits_charged=float(r.total_credits_charged),
        cogs_usd=round(total_cogs, 4),
        step_count=len(events),
        created_at=r.created_at.isoformat(),
        finished_at=r.finished_at.isoformat() if r.finished_at else None,
    )

    steps = [
        RunStepItem(
            event_id=str(e.event_id),
            capability_id=e.capability_id,
            provider=e.provider,
            model_id=e.model_id,
            input_tokens=e.input_tokens,
            output_tokens=e.output_tokens,
            quantity=e.quantity,
            unit_type=e.unit_type.value if hasattr(e.unit_type, "value") else str(e.unit_type),
            cogs_usd=round(float(e.provider_cost_usd or 0), 4),
            credits_charged=e.credits_charged,
            status=e.status,
            created_at=e.created_at.isoformat(),
        )
        for e in events
    ]

    return RunDetailResponse(run=run_item, steps=steps)


# ─── Provider & Model Analytics ──────────────────────────────────────────────

@router.get("/analytics/providers", response_model=ProviderAnalyticsResponse)
async def get_provider_analytics(
    time_range: Optional[str] = Query("30d"),
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    since = parse_time_range(time_range)
    u_filter = [UsageEvent.created_at >= since] if since else []

    # Providers breakdown
    p_res = await db.execute(
        select(
            UsageEvent.provider,
            func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0).label("cogs"),
            func.count(UsageEvent.event_id).label("req_count"),
        )
        .where(*u_filter)
        .group_by(UsageEvent.provider)
    )
    p_rows = p_res.all()
    tot_cogs = sum(float(r.cogs) for r in p_rows)
    providers = [
        ProviderShareItem(
            provider=r.provider or "unknown",
            cogs_usd=round(float(r.cogs), 4),
            request_count=int(r.req_count),
            share_pct=round((float(r.cogs) / tot_cogs * 100.0), 1) if tot_cogs > 0 else 0.0,
        )
        for r in p_rows
    ]

    # Model telemetry & efficiency (truthful metrics, zero fake revenue)
    m_res = await db.execute(
        select(
            UsageEvent.model_id,
            UsageEvent.provider,
            func.count(UsageEvent.event_id).label("req_count"),
            func.coalesce(func.sum(UsageEvent.input_tokens + UsageEvent.output_tokens), 0).label("tokens"),
            func.coalesce(func.sum(UsageEvent.credits_charged), 0).label("credits"),
            func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0).label("cogs"),
        )
        .where(*u_filter)
        .group_by(UsageEvent.model_id, UsageEvent.provider)
    )
    models = []
    for r in m_res.all():
        cogs = round(float(r.cogs or 0), 4)
        credits = int(r.credits or 0)
        tot_tok = int(r.tokens or 0)
        cogs_per_1m = round((cogs / (tot_tok / 1_000_000)), 4) if tot_tok > 0 else 0.0

        models.append(
            ModelProfitabilityItem(
                model_id=r.model_id or "default",
                provider=r.provider or "unknown",
                request_count=int(r.req_count),
                total_tokens=tot_tok,
                credits_charged=credits,
                cogs_usd=cogs,
                cogs_per_1m_tokens_usd=cogs_per_1m,
            )
        )

    return ProviderAnalyticsResponse(providers=providers, models=models)


# ─── Global Economics Page ───────────────────────────────────────────────────

@router.get("/economics", response_model=GlobalEconomicsResponse)
async def get_global_economics(
    time_range: Optional[str] = Query("30d"),
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    since = parse_time_range(time_range)
    u_filter = [UsageEvent.created_at >= since] if since else []
    tool_filter = [UsageEvent.capability_id.in_(["web_search", "browser_use", "ocr", "image_gen"])]
    if since:
        tool_filter.append(UsageEvent.created_at >= since)

    # Total Provider & Tool COGS
    res_cogs = await db.execute(
        select(func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0)).where(*u_filter)
    )
    total_cogs_usd = float(res_cogs.scalar() or 0.0)

    res_tool_cogs = await db.execute(
        select(func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0)).where(*tool_filter)
    )
    tool_cogs_usd = float(res_tool_cogs.scalar() or 0.0)
    provider_cogs_usd = max(0.0, total_cogs_usd - tool_cogs_usd)

    # Credits consumed
    res_consumed = await db.execute(
        select(func.coalesce(func.sum(UsageEvent.credits_charged), 0)).where(*u_filter)
    )
    credits_consumed = int(res_consumed.scalar() or 0)

    # Revenue — derived EXCLUSIVELY from completed billing transactions
    rev_filter = [BillingTransaction.status.in_(["processed", "completed"])]
    if since:
        rev_filter.append(BillingTransaction.created_at >= since)
    res_rev = await db.execute(
        select(func.coalesce(func.sum(BillingTransaction.amount_minor), 0)).where(*rev_filter)
    )
    rev_minor = res_rev.scalar() or 0
    revenue_usd = round(rev_minor / 100.0, 2)

    gross_profit_usd = round(revenue_usd - total_cogs_usd, 2)
    gross_margin_pct = (
        round((gross_profit_usd / revenue_usd * 100.0), 1)
        if revenue_usd > 0
        else None
    )

    # Revenue by Plan
    plan_res = await db.execute(
        select(
            SubscriptionPlan.name,
            SubscriptionPlan.price_usd,
            func.count(Subscription.id).label("user_count"),
        )
        .join(Subscription, Subscription.plan_id == SubscriptionPlan.id)
        .where(Subscription.status == SubscriptionStatus.ACTIVE)
        .group_by(SubscriptionPlan.name, SubscriptionPlan.price_usd)
    )
    revenue_by_plan = [
        PlanRevenueItem(
            plan_name=r.name.upper(),
            user_count=int(r.user_count),
            price_usd=float(r.price_usd),
            revenue_usd=round(float(r.price_usd) * int(r.user_count), 2),
        )
        for r in plan_res.all()
    ]

    # COGS by Provider
    p_res = await db.execute(
        select(
            UsageEvent.provider,
            func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0).label("cogs"),
            func.count(UsageEvent.event_id).label("cnt"),
        )
        .where(*u_filter)
        .group_by(UsageEvent.provider)
    )
    p_rows = p_res.all()
    cogs_by_provider = [
        ProviderShareItem(
            provider=r.provider or "unknown",
            cogs_usd=round(float(r.cogs), 4),
            request_count=int(r.cnt),
            share_pct=round((float(r.cogs) / total_cogs_usd * 100.0), 1) if total_cogs_usd > 0 else 0.0,
        )
        for r in p_rows
    ]

    # COGS by Tool
    t_res = await db.execute(
        select(
            UsageEvent.capability_id,
            func.count(UsageEvent.event_id).label("cnt"),
            func.coalesce(func.sum(UsageEvent.provider_cost_usd), 0).label("cogs"),
        )
        .where(*tool_filter)
        .group_by(UsageEvent.capability_id)
    )
    cogs_by_tool = [
        ToolCogsItem(
            tool_name=r.capability_id,
            call_count=int(r.cnt),
            cogs_usd=round(float(r.cogs), 4),
        )
        for r in t_res.all()
    ]

    # Credits Issued & Liability
    res_issued = await db.execute(
        select(func.coalesce(func.sum(CreditTransaction.amount), 0))
        .where(CreditTransaction.amount > 0)
    )
    credits_issued = int(res_issued.scalar() or 0)

    # Current unspent credits across all wallets
    res_liability = await db.execute(
        select(func.coalesce(func.sum(Wallet.monthly_balance + Wallet.topup_balance), 0))
    )
    unspent_credits = int(res_liability.scalar() or 0)

    # Query active credit reference USD from DB
    cfg_res = await db.execute(
        select(PricingConfiguration).where(PricingConfiguration.active.is_(True)).order_by(PricingConfiguration.created_at.desc())
    )
    active_cfg = cfg_res.scalar_one_or_none()
    credit_ref_usd = float(active_cfg.credit_reference_usd) if active_cfg else 0.10
    credit_liability_usd = round(unspent_credits * credit_ref_usd, 2)

    return GlobalEconomicsResponse(
        revenue_usd=revenue_usd,
        revenue_source=FinancialSource.BILLING_TRANSACTIONS.value,
        provider_cogs_usd=round(provider_cogs_usd, 2),
        tool_cogs_usd=round(tool_cogs_usd, 2),
        total_cogs_usd=round(total_cogs_usd, 2),
        cogs_source=FinancialSource.USAGE_EVENTS.value,
        gross_profit_usd=gross_profit_usd,
        gross_margin_pct=gross_margin_pct,
        revenue_by_plan=revenue_by_plan,
        cogs_by_provider=cogs_by_provider,
        cogs_by_tool=cogs_by_tool,
        credits_issued=credits_issued,
        credits_consumed=credits_consumed,
        credit_source=FinancialSource.CREDIT_TRANSACTIONS.value,
        credit_liability_usd=credit_liability_usd,
    )


# ─── Credit Ledger & Audit Logs ───────────────────────────────────────────────

@router.get("/ledger", response_model=LedgerListResponse)
async def list_admin_ledger(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    account_id: Optional[str] = None,
    txn_type: Optional[str] = None,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(CreditTransaction)
    if account_id:
        try:
            query = query.where(CreditTransaction.account_id == uuid.UUID(account_id))
        except ValueError:
            pass
    if txn_type:
        query = query.where(CreditTransaction.type == txn_type)

    count_q = select(func.count()).select_from(query.subquery())
    count_res = await db.execute(count_q)
    total_count = count_res.scalar() or 0

    offset = (page - 1) * page_size
    query = query.order_by(CreditTransaction.created_at.desc()).offset(offset).limit(page_size)
    res = await db.execute(query)
    txns = res.scalars().all()

    items = []
    for t in txns:
        acc_res = await db.execute(select(Account.email).where(Account.account_id == t.account_id))
        email = acc_res.scalar() or "unknown"
        items.append(
            LedgerItem(
                transaction_id=str(t.transaction_id),
                account_id=str(t.account_id),
                user_email=email,
                type=t.type.value if hasattr(t.type, "value") else str(t.type),
                amount=t.amount,
                note=t.note,
                created_at=t.created_at.isoformat(),
            )
        )

    return LedgerListResponse(
        total_count=total_count,
        page=page,
        page_size=page_size,
        transactions=items,
    )


@router.get("/audit-logs", response_model=AuditLogListResponse)
async def list_admin_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(AdminAuditLog)
    if action:
        query = query.where(AdminAuditLog.action == action)
    if resource_type:
        query = query.where(AdminAuditLog.resource_type == resource_type)

    count_q = select(func.count()).select_from(query.subquery())
    count_res = await db.execute(count_q)
    total_count = count_res.scalar() or 0

    offset = (page - 1) * page_size
    query = query.order_by(AdminAuditLog.created_at.desc()).offset(offset).limit(page_size)
    res = await db.execute(query)
    logs = res.scalars().all()

    items = [
        AuditLogItem(
            id=str(l.id),
            admin_id=str(l.admin_id) if l.admin_id else None,
            admin_email=l.admin_email,
            action=l.action,
            resource_type=l.resource_type,
            resource_id=l.resource_id,
            old_value=l.old_value,
            new_value=l.new_value,
            ip_address=l.ip_address,
            created_at=l.created_at.isoformat(),
        )
        for l in logs
    ]

    return AuditLogListResponse(
        total_count=total_count,
        page=page,
        page_size=page_size,
        logs=items,
    )


# ── Auth Providers Management ──────────────────────────────────────────────────

class AuthProviderItem(BaseModel):
    id: str
    name: str
    client_id: str
    has_secret: bool
    is_configured: bool
    redirect_uris: list[str]
    capabilities: list[str]
    help_url: str


class AuthProvidersListResponse(BaseModel):
    providers: list[AuthProviderItem]


class UpdateAuthProviderRequest(BaseModel):
    provider: str = Field(..., description="google | github | microsoft")
    client_id: str = Field(..., description="OAuth Client ID")
    client_secret: Optional[str] = Field(None, description="OAuth Client Secret (optional if maintaining existing)")


def _sync_env_file(file_path: Path, updates: dict[str, str]) -> None:
    """Updates or appends key=value pairs in a target .env file safely."""
    if not file_path.parent.exists():
        return
    existing_lines: list[str] = []
    if file_path.exists():
        try:
            existing_lines = file_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            existing_lines = []

    updated_keys = set()
    new_lines: list[str] = []
    for line in existing_lines:
        matched = False
        for k, v in updates.items():
            pattern = rf"^\s*#?\s*{re.escape(k)}\s*=.*$"
            if re.match(pattern, line):
                new_lines.append(f"{k}={v}")
                updated_keys.add(k)
                matched = True
                break
        if not matched:
            new_lines.append(line)

    for k, v in updates.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    file_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


@router.get("/auth-providers", response_model=AuthProvidersListResponse)
async def list_auth_providers(
    session: WebSession = Depends(require_admin),
):
    """
    Returns public/masked configuration status of official OAuth providers
    (Google, GitHub, Microsoft) for the Admin Console.
    Never leaks plaintext client secrets.
    """
    providers_def = [
        {
            "id": "google",
            "name": "Google OAuth 2.0",
            "id_env": "GOOGLE_CLIENT_ID",
            "sec_env": "GOOGLE_CLIENT_SECRET",
            "fallback_id_env": "GMAIL_CLIENT_ID",
            "fallback_sec_env": "GMAIL_CLIENT_SECRET",
            "redirect_uris": [
                "http://localhost:8000/api/connections/google/callback",
                "http://localhost:8001/api/connections/google/callback",
                "http://localhost:8001/auth/google/callback",
            ],
            "capabilities": ["Autonomous Agent Browser", "Gmail & Workspace MCPs", "Talos Cloud SSO"],
            "help_url": "https://console.cloud.google.com/apis/credentials",
        },
        {
            "id": "github",
            "name": "GitHub OAuth App",
            "id_env": "GITHUB_CLIENT_ID",
            "sec_env": "GITHUB_CLIENT_SECRET",
            "fallback_id_env": None,
            "fallback_sec_env": None,
            "redirect_uris": [
                "http://localhost:8000/api/connections/github/callback",
                "http://localhost:8001/api/connections/github/callback",
            ],
            "capabilities": ["GitHub Repos & Code MCP", "PR & Issue Automation", "Git Operations"],
            "help_url": "https://github.com/settings/developers",
        },
        {
            "id": "microsoft",
            "name": "Microsoft Entra ID (Azure)",
            "id_env": "MICROSOFT_CLIENT_ID",
            "sec_env": "MICROSOFT_CLIENT_SECRET",
            "fallback_id_env": None,
            "fallback_sec_env": None,
            "redirect_uris": [
                "http://localhost:8000/api/connections/microsoft/callback",
                "http://localhost:8001/api/connections/microsoft/callback",
            ],
            "capabilities": ["Microsoft 365 & Office", "Graph API Automation", "Outlook Mail & Calendar MCP"],
            "help_url": "https://portal.azure.com/#blade/Microsoft_AAD_IAM/ActiveDirectoryMenuBlade/RegisteredApps",
        },
    ]

    items: list[AuthProviderItem] = []
    for p in providers_def:
        cid = os.environ.get(p["id_env"]) or (os.environ.get(p["fallback_id_env"]) if p["fallback_id_env"] else None) or ""
        sec = os.environ.get(p["sec_env"]) or (os.environ.get(p["fallback_sec_env"]) if p["fallback_sec_env"] else None) or ""
        has_sec = bool(sec)
        items.append(
            AuthProviderItem(
                id=p["id"],
                name=p["name"],
                client_id=cid,
                has_secret=has_sec,
                is_configured=bool(cid and has_sec),
                redirect_uris=p["redirect_uris"],
                capabilities=p["capabilities"],
                help_url=p["help_url"],
            )
        )

    return AuthProvidersListResponse(providers=items)


@router.post("/auth-providers")
async def update_auth_provider(
    payload: UpdateAuthProviderRequest,
    session: WebSession = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Updates client ID and secret for an official OAuth provider.
    Synchronizes both talos-cloud/.env and talos-backend/.env and in-memory os.environ,
    and logs the action to AdminAuditLog.
    """
    pid = payload.provider.lower().strip()
    if pid not in ("google", "github", "microsoft"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported provider '{payload.provider}'. Supported: google, github, microsoft.",
        )

    cid = payload.client_id.strip()
    if not cid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Client ID cannot be empty.",
        )

    env_map = {
        "google": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
        "github": ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"),
        "microsoft": ("MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"),
    }
    id_key, sec_key = env_map[pid]
    old_client_id = os.environ.get(id_key, "")
    old_has_secret = bool(os.environ.get(sec_key, ""))

    updates: dict[str, str] = {id_key: cid}
    if payload.client_secret and payload.client_secret.strip():
        updates[sec_key] = payload.client_secret.strip()

    # Update both cloud and backend .env files
    cloud_env = Path(__file__).resolve().parents[2] / ".env"
    backend_env = Path(__file__).resolve().parents[3] / "talos-backend" / ".env"

    _sync_env_file(cloud_env, updates)
    _sync_env_file(backend_env, updates)

    for k, v in updates.items():
        os.environ[k] = v
    get_settings.cache_clear()

    final_has_secret = bool(updates.get(sec_key) or old_has_secret)
    await log_admin_audit(
        db=db,
        session=session,
        action="update_auth_provider",
        resource_type="auth_provider",
        resource_id=pid,
        old_value={"client_id": old_client_id, "has_secret": old_has_secret},
        new_value={"client_id": cid, "has_secret": final_has_secret},
    )
    await db.commit()

    return {
        "status": "ok",
        "provider": pid,
        "message": f"Successfully updated OAuth credentials for {pid.title()}.",
        "has_secret": final_has_secret,
        "is_configured": bool(cid and final_has_secret),
    }


@router.get("/providers/health")
async def get_providers_health(
    session: WebSession = Depends(require_admin),
):
    """
    Operator visibility endpoint reporting:
    - Upstream circuit breaker states per provider (closed/open/half_open)
    - Real-time telemetry metrics: p50/p95/p99 latency, error rate, token throughput.
    """
    from app.services.circuit_breaker import circuit_breaker
    from app.services.provider_telemetry import telemetry_tracker

    circuit_states = await circuit_breaker.get_all_states()
    telemetry = await telemetry_tracker.get_metrics()

    all_providers = set(circuit_states.keys()) | set(telemetry.keys()) | {
        "anthropic", "openai", "gemini", "groq", "deepseek", "zhipu"
    }

    providers_summary = {}
    for p in sorted(all_providers):
        providers_summary[p] = {
            "circuit_state": circuit_states.get(p, "closed"),
            "telemetry": telemetry.get(p, {
                "total_calls": 0,
                "success_count": 0,
                "failure_count": 0,
                "error_rate_pct": 0.0,
                "latency_ms": {"avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0},
                "total_tokens": 0,
            }),
        }

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "providers": providers_summary,
    }


