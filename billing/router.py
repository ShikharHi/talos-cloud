from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from decimal import Decimal

from auth.service import get_current_user
from billing.repository import billing_repo
from config import config

router = APIRouter(prefix="/v1/billing", tags=["Billing & Credits"])

@router.get("/balance")
async def get_user_balance(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Fetch live credit balance, active reservations, spendable credits, and subscription status."""
    user_id = current_user["id"]
    wallet = await billing_repo.get_wallet(user_id)
    subscription = await billing_repo.get_subscription(user_id)

    settled = Decimal(str(wallet.get("settled_balance", "0")))
    reserved = Decimal(str(wallet.get("active_reserved", "0")))
    spendable = max(Decimal("0"), settled - reserved)

    plan_info = {
        "plan_name": subscription.get("plan_name", "free") if subscription else "free",
        "status": subscription.get("status", "unsubscribed") if subscription else "unsubscribed",
        "monthly_allowance": float(subscription.get("monthly_credit_allowance", 5000.0)) if subscription else 0.0,
        "period_end": subscription.get("period_end") if subscription else None,
        "actual_cogs_usd": float(subscription.get("period_actual_cogs_usd", 0.0)) if subscription else 0.0,
        "cogs_ceiling_usd": float(subscription.get("period_cogs_ceiling_usd", 5.0)) if subscription else 0.0,
    }

    return {
        "status": "success",
        "user_id": user_id,
        "plan": plan_info,
        "balance": {
            "settled_credits": float(settled),
            "active_reserved_credits": float(reserved),
            "spendable_credits": float(spendable)
        }
    }

@router.get("/history")
async def get_user_ledger_history(
    limit: int = 50,
    offset: int = 0,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Fetch paginated credit transaction history."""
    user_id = current_user["id"]
    with billing_repo._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM credit_ledger 
                WHERE user_id = %s 
                ORDER BY created_at DESC 
                LIMIT %s OFFSET %s
            """, (user_id, limit, offset))
            rows = cur.fetchall()
            return {"status": "success", "transactions": [dict(r) for r in rows]}

# ---------------------------------------------------------------------------
# INTERNAL SERVICE ENDPOINTS (Guarded by X-Talos-Internal-Token)
# ---------------------------------------------------------------------------

class ReserveRequest(BaseModel):
    user_id: str
    thread_id: Optional[str] = None
    operation_type: str
    amount_credits: float
    estimated_cogs_usd: float = 0.0
    idempotency_key: str
    ttl_seconds: int = 120

class SettleRequest(BaseModel):
    reservation_id: str
    actual_credits: float
    raw_cost_usd: float
    pricing_version: str
    idempotency_key: str
    meta: Optional[Dict[str, Any]] = None

@router.post("/internal/reserve")
async def internal_reserve_endpoint(req: ReserveRequest):
    try:
        res = await billing_repo.reserve_credits(
            user_id=req.user_id,
            thread_id=req.thread_id,
            operation_type=req.operation_type,
            amount_credits=Decimal(str(req.amount_credits)),
            estimated_cogs_usd=Decimal(str(req.estimated_cogs_usd)),
            idempotency_key=req.idempotency_key,
            ttl_seconds=req.ttl_seconds
        )
        return {"status": "success", "reservation": res}
    except Exception as e:
        raise HTTPException(status_code=402, detail=str(e))

@router.post("/internal/settle")
async def internal_settle_endpoint(req: SettleRequest):
    try:
        settlement = await billing_repo.settle_reservation(
            reservation_id=req.reservation_id,
            actual_credits=Decimal(str(req.actual_credits)),
            raw_cost_usd=Decimal(str(req.raw_cost_usd)),
            pricing_version=req.pricing_version,
            idempotency_key=req.idempotency_key,
            meta=req.meta
        )
        return {"status": "success", "settlement": settlement}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
