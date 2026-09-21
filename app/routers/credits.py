"""
Talos Cloud — Credits router (Phase 3).

Exposes:
  GET /credits/balance        — current balance split by subscription vs top-up
  GET /credits/transactions   — paginated transaction history

INVARIANT: No field in any response here contains a credit-to-currency conversion.
No "1 credit = ₹X" or "usd_per_credit" anywhere.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.ledger import CreditTransaction
from app.routers.relay import get_authenticated_account
from app.services import ledger_service

router = APIRouter(prefix="/credits", tags=["credits"])


class BalanceResponse(BaseModel):
    subscription_credits: int
    topup_credits: int
    total_credits: int
    # No currency field. See module docstring.


class TransactionRecord(BaseModel):
    transaction_id: str
    type: str
    amount: int
    note: str | None
    created_at: str


class TransactionsResponse(BaseModel):
    transactions: list[TransactionRecord]
    total: int


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    account=Depends(get_authenticated_account),
    db: AsyncSession = Depends(get_db),
):
    """Returns the current balance split into subscription and top-up components."""
    breakdown = await ledger_service.get_balance_breakdown(db, account.account_id)
    if "error" in breakdown:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=breakdown["error"])
    return BalanceResponse(**breakdown)


@router.get("/transactions", response_model=TransactionsResponse)
async def list_transactions(
    account=Depends(get_authenticated_account),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Returns paginated transaction history for the authenticated account."""
    result = await db.execute(
        select(CreditTransaction)
        .where(CreditTransaction.account_id == account.account_id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    txns = result.scalars().all()

    from sqlalchemy import func
    count_result = await db.execute(
        select(func.count()).where(CreditTransaction.account_id == account.account_id)
    )
    total = count_result.scalar_one()

    return TransactionsResponse(
        transactions=[
            TransactionRecord(
                transaction_id=str(t.transaction_id),
                type=t.type.value,
                amount=t.amount,
                note=t.note,
                created_at=t.created_at.isoformat(),
            )
            for t in txns
        ],
        total=total,
    )
