"""
Talos Cloud — Credit Ledger Service.

Responsible for:
  - Granting subscription credits at cycle start
  - Granting top-up credits on purchase
  - Expiring unused subscription credits at cycle end
  - Querying current balance with correct consumption ordering from Wallet
  - Auditable balance: wallet.monthly_balance + wallet.topup_balance must equal
    SUM(amount) over credit_transactions for that account at all times

Consumption order (enforced by WalletEngine):
  Subscription credits (monthly_balance) consumed FIRST.
  Top-up credits (topup_balance) consumed SECOND (and never expire).

Credit-to-currency: NEVER exposed in any public field or endpoint.
"""

import uuid
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ledger import CreditTransaction, TransactionType
from app.models.wallet import Wallet
from app.services.wallet_engine import WalletEngine


CreditKind = Literal["subscription", "topup"]


async def grant_subscription_credits(
    db: AsyncSession,
    account_id: uuid.UUID,
    credits: int,
    cycle_ref: str,
) -> CreditTransaction | None:
    """
    Grants subscription credits at the start of a billing cycle.
    Resets monthly_balance via WalletEngine and records the transaction.
    """
    engine = WalletEngine(db)
    await engine.apply_monthly_grant(account_id=account_id, grant_amount=credits, cycle_ref=cycle_ref)

    # Return the transaction written by WalletEngine
    result = await db.execute(
        select(CreditTransaction)
        .where(
            CreditTransaction.account_id == account_id,
            CreditTransaction.type == TransactionType.subscription_grant,
        )
        .order_by(CreditTransaction.created_at.desc())
    )
    return result.scalars().first()


async def grant_topup_credits(
    db: AsyncSession,
    account_id: uuid.UUID,
    credits: int,
    purchase_ref: str,
) -> CreditTransaction | None:
    """
    Grants top-up credits on purchase.
    Adds to topup_balance via WalletEngine (never expires).
    """
    engine = WalletEngine(db)
    await engine.credit_topup(account_id=account_id, amount=credits, purchase_ref=purchase_ref)

    result = await db.execute(
        select(CreditTransaction)
        .where(
            CreditTransaction.account_id == account_id,
            CreditTransaction.type == TransactionType.topup,
        )
        .order_by(CreditTransaction.created_at.desc())
    )
    return result.scalars().first()


async def expire_subscription_credits(
    db: AsyncSession,
    account_id: uuid.UUID,
    cycle_ref: str,
) -> CreditTransaction | None:
    """
    Expires unused subscription credits at the end of a billing cycle.
    Delegates to WalletEngine.apply_monthly_grant which handles expiry.
    """
    result = await db.execute(
        select(Wallet).where(Wallet.account_id == account_id)
    )
    wallet = result.scalar_one_or_none()
    if wallet is None or wallet.monthly_balance <= 0:
        return None

    expired_amount = wallet.monthly_balance
    engine = WalletEngine(db)
    await engine.apply_monthly_grant(account_id=account_id, grant_amount=0, cycle_ref=cycle_ref)

    result_txn = await db.execute(
        select(CreditTransaction)
        .where(
            CreditTransaction.account_id == account_id,
            CreditTransaction.type == TransactionType.subscription_expiry,
        )
        .order_by(CreditTransaction.created_at.desc())
    )
    return result_txn.scalars().first()


async def get_balance_breakdown(
    db: AsyncSession,
    account_id: uuid.UUID,
) -> dict:
    """
    Returns the current balance split into subscription and top-up components.
    Source of truth: Wallet table.
    """
    result = await db.execute(
        select(Wallet).where(Wallet.account_id == account_id)
    )
    wallet = result.scalar_one_or_none()
    if wallet is None:
        return {
            "subscription_credits": 0,
            "topup_credits": 0,
            "total_credits": 0,
        }

    return {
        "subscription_credits": wallet.monthly_balance,
        "topup_credits": wallet.topup_balance,
        "total_credits": wallet.monthly_balance + wallet.topup_balance,
    }


async def verify_ledger_auditability(
    db: AsyncSession,
    account_id: uuid.UUID,
) -> bool:
    """
    Verifies that SUM(credit_transactions.amount) matches wallet.monthly_balance + wallet.topup_balance.
    Used in tests and admin tooling to assert ledger integrity.
    Returns True if the ledger is consistent.
    """
    await db.flush()

    result = await db.execute(
        select(Wallet).where(Wallet.account_id == account_id)
    )
    wallet = result.scalar_one_or_none()
    if wallet is None:
        return False

    wallet_total = wallet.monthly_balance + wallet.topup_balance

    result_sum = await db.execute(
        select(func.coalesce(func.sum(CreditTransaction.amount), 0)).where(
            CreditTransaction.account_id == account_id
        )
    )
    ledger_sum = result_sum.scalar_one() or 0

    return wallet_total == ledger_sum
