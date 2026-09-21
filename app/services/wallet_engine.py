"""
Talos Cloud — Wallet Engine (Phase 3 Financial Core).

THE ONLY authorized mutator of Wallet balances and reservations.

CRITICAL INVARIANTS:
  1. Wallet can NEVER go negative on either monthly_balance or topup_balance.
  2. Split availability tracking:
       available_monthly = monthly_balance - reserved_monthly
       available_topup = topup_balance - reserved_topup
       available_credits = available_monthly + available_topup
  3. All mutations happen inside a single database transaction with
     SELECT ... FOR UPDATE row-level locking on the Wallet row.
  4. reserve() is an authorization hold, NOT a spend. It increments
     reserved_monthly and reserved_topup. No ledger entries are written on reserve().
  5. commit() charges actual_amount, decrements settled balances, releases
     the reservation hold, and records a single signed relay_spend entry in the ledger.
  6. release() releases the held reservation amounts without mutating settled balances.
  7. No direct UPDATE to Wallet from anywhere outside this module.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import Account
from app.models.wallet import CreditReservation, ReservationStatus, Wallet
from app.models.ledger import CreditTransaction, TransactionType

# Reservation TTL: held reservations expire after this duration
RESERVATION_TTL_MINUTES = 30

# Per-account asyncio locks for local/in-memory concurrency (e.g. SQLite tests)
_account_locks: Dict[str, asyncio.Lock] = {}
_locks_registry_lock = asyncio.Lock()


async def _get_account_lock(account_id: uuid.UUID) -> asyncio.Lock:
    key = str(account_id)
    lock = _account_locks.get(key)
    if lock is None:
        async with _locks_registry_lock:
            lock = _account_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                _account_locks[key] = lock
    return lock


class InsufficientCreditsError(Exception):
    """Raised when the wallet lacks credits for the requested amount."""
    def __init__(self, account_id: str, required: int, current: int = 0, current_monthly: int = 0, current_topup: int = 0):
        self.account_id = account_id
        self.required = required
        self.current = current or (current_monthly + current_topup)
        self.current_monthly = current_monthly
        self.current_topup = current_topup
        self.current_total = self.current
        super().__init__(
            f"Insufficient credits: need {required}, have {self.current}"
        )


class WalletEngine:
    """
    All Wallet mutations go through this class.
    Never call db.execute(UPDATE wallets ...) directly elsewhere.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    # ─── Public API ────────────────────────────────────────────────────────────

    async def get_wallet(self, account_id: uuid.UUID) -> Wallet:
        """Returns the Wallet for the given account. Auto-creates if account exists."""
        result = await self.db.execute(
            select(Wallet).where(Wallet.account_id == account_id)
        )
        wallet = result.scalar_one_or_none()
        if wallet is None:
            acc_res = await self.db.execute(select(Account).where(Account.account_id == account_id))
            acc = acc_res.scalar_one_or_none()
            if acc is not None:
                initial_bal = acc.balance_credits if (acc.balance_credits and acc.balance_credits > 0) else 0
                wallet = Wallet(
                    account_id=account_id,
                    monthly_balance=initial_bal,
                    topup_balance=0,
                    monthly_grant=initial_bal,
                    reserved_monthly=0,
                    reserved_topup=0,
                )
                self.db.add(wallet)
                await self.db.flush()
            else:
                raise ValueError(f"No account or wallet found for account {account_id}")
        return wallet

    async def get_balance(self, account_id: uuid.UUID) -> dict:
        """
        Returns the current balance breakdown based on unreserved available credits.
        Does NOT expose internal_credit_reference or USD equivalents.
        """
        wallet = await self.get_wallet(account_id)
        return {
            "monthly_credits": wallet.available_monthly,
            "topup_credits": wallet.available_topup,
            "total_credits": wallet.available_credits,
            "reserved_monthly": wallet.reserved_monthly,
            "reserved_topup": wallet.reserved_topup,
            "total_reserved": wallet.reserved_monthly + wallet.reserved_topup,
            "settled_monthly": wallet.monthly_balance,
            "settled_topup": wallet.topup_balance,
        }

    async def create_wallet(
        self,
        account_id: uuid.UUID,
        initial_monthly: int = 0,
        initial_topup: int = 0,
    ) -> Wallet:
        """
        Creates a new Wallet for an account.
        Called once when a new account is created or a subscription is first enrolled.
        """
        wallet = Wallet(
            account_id=account_id,
            monthly_balance=initial_monthly,
            topup_balance=initial_topup,
            monthly_grant=initial_monthly,
            reserved_monthly=0,
            reserved_topup=0,
        )
        self.db.add(wallet)
        await self.db.flush()
        return wallet

    async def reserve(
        self,
        account_id: uuid.UUID,
        task_id: str | None,
        amount: int,
        idempotency_key: str | None = None,
    ) -> CreditReservation:
        """
        Atomic credit reservation (authorization hold) prior to provider execution.
        Allocates from unreserved monthly balance first, then topup balance.
        Zero ledger entries are written on reserve().
        """
        if idempotency_key:
            existing = await self.db.execute(
                select(CreditReservation).where(
                    CreditReservation.idempotency_key == idempotency_key,
                    CreditReservation.status == ReservationStatus.HELD,
                )
            )
            existing_res = existing.scalar_one_or_none()
            if existing_res is not None:
                return existing_res

        _lock = await _get_account_lock(account_id)
        async with _lock:
            if idempotency_key:
                existing = await self.db.execute(
                    select(CreditReservation).where(
                        CreditReservation.idempotency_key == idempotency_key,
                        CreditReservation.status == ReservationStatus.HELD,
                    )
                )
                existing_res = existing.scalar_one_or_none()
                if existing_res is not None:
                    return existing_res

            self.db.expire_all()

            # Row lock wallet via SELECT ... FOR UPDATE
            stmt = select(Wallet).where(Wallet.account_id == account_id).with_for_update()
            w_res = await self.db.execute(stmt)
            wallet = w_res.scalar_one_or_none()

            if wallet is None:
                wallet = await self.get_wallet(account_id)

            avail_monthly = wallet.available_monthly
            avail_topup = wallet.available_topup
            total_avail = avail_monthly + avail_topup

            if total_avail < amount:
                raise InsufficientCreditsError(
                    account_id=str(account_id),
                    required=amount,
                    current=total_avail,
                    current_monthly=avail_monthly,
                    current_topup=avail_topup,
                )

            # Determine split allocation from unreserved funds
            take_monthly = min(avail_monthly, amount)
            take_topup = amount - take_monthly

            wallet.reserved_monthly += take_monthly
            wallet.reserved_topup += take_topup
            wallet.version += 1

            reservation = CreditReservation(
                wallet_id=wallet.wallet_id,
                task_id=task_id,
                monthly_reserved=take_monthly,
                topup_reserved=take_topup,
                amount_reserved=amount,
                status=ReservationStatus.HELD,
                idempotency_key=idempotency_key,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=RESERVATION_TTL_MINUTES),
            )
            self.db.add(reservation)
            await self.db.flush()
            await self.db.commit()
            return reservation

    async def commit(
        self,
        reservation_id: uuid.UUID,
        actual_amount: int,
    ) -> None:
        """
        Settles actual usage against a HELD reservation.
        Deducts actual spend from settled balances, releases the hold,
        and records a signed relay_spend transaction in the append-only ledger.
        """
        self.db.expire_all()
        result = await self.db.execute(
            select(CreditReservation)
            .where(CreditReservation.reservation_id == reservation_id)
            .with_for_update()
        )
        reservation = result.scalar_one_or_none()
        if reservation is None:
            raise ValueError(f"Reservation {reservation_id} not found")
        if reservation.status != ReservationStatus.HELD:
            raise ValueError(
                f"Reservation {reservation_id} is {reservation.status}, not HELD. "
                "Cannot commit."
            )

        wallet_result = await self.db.execute(
            select(Wallet)
            .where(Wallet.wallet_id == reservation.wallet_id)
            .with_for_update()
        )
        wallet = wallet_result.scalar_one()

        # Allocate actual spend against reserved sources
        actual_monthly = min(reservation.monthly_reserved, actual_amount)
        actual_topup = actual_amount - actual_monthly

        # Deduct from settled balances
        wallet.monthly_balance = max(0, wallet.monthly_balance - actual_monthly)
        wallet.topup_balance = max(0, wallet.topup_balance - actual_topup)

        # Release the entire hold
        wallet.reserved_monthly = max(0, wallet.reserved_monthly - reservation.monthly_reserved)
        wallet.reserved_topup = max(0, wallet.reserved_topup - reservation.topup_reserved)
        wallet.version += 1

        # Record in append-only signed ledger
        account_id = wallet.account_id
        if actual_amount > 0:
            await self._write_credit_transaction(
                account_id=account_id,
                wallet_id=wallet.wallet_id,
                task_id=reservation.task_id,
                txn_type=TransactionType.relay_spend,
                action_type="relay_spend",
                amount=-actual_amount,
                balance_after_monthly=wallet.monthly_balance,
                balance_after_topup=wallet.topup_balance,
                note=f"relay spend {actual_amount} credits (reserved={reservation.amount_reserved})",
            )

        reservation.status = ReservationStatus.COMMITTED
        reservation.amount_committed = actual_amount
        reservation.released_at = datetime.now(timezone.utc)
        await self.db.flush()

    async def release(self, reservation_id: uuid.UUID) -> None:
        """
        Releases a HELD reservation without spending credits (e.g. upstream provider failure).
        """
        result = await self.db.execute(
            select(CreditReservation)
            .where(CreditReservation.reservation_id == reservation_id)
            .with_for_update()
        )
        reservation = result.scalar_one_or_none()
        if reservation is None:
            raise ValueError(f"Reservation {reservation_id} not found")
        if reservation.status != ReservationStatus.HELD:
            return

        wallet_result = await self.db.execute(
            select(Wallet)
            .where(Wallet.wallet_id == reservation.wallet_id)
            .with_for_update()
        )
        wallet = wallet_result.scalar_one()

        # Release holds back to available pool
        wallet.reserved_monthly = max(0, wallet.reserved_monthly - reservation.monthly_reserved)
        wallet.reserved_topup = max(0, wallet.reserved_topup - reservation.topup_reserved)
        wallet.version += 1

        reservation.status = ReservationStatus.RELEASED
        reservation.released_at = datetime.now(timezone.utc)
        await self.db.flush()

    async def credit_topup(
        self,
        account_id: uuid.UUID,
        amount: int,
        purchase_ref: str,
    ) -> None:
        """Adds topup credits to the wallet and records a topup ledger entry."""
        wallet = await self.get_wallet(account_id)
        wallet.topup_balance += amount
        wallet.version += 1

        await self._write_credit_transaction(
            account_id=account_id,
            wallet_id=wallet.wallet_id,
            task_id=None,
            txn_type=TransactionType.topup,
            action_type="topup",
            amount=amount,
            balance_after_monthly=wallet.monthly_balance,
            balance_after_topup=wallet.topup_balance,
            reference_id=purchase_ref,
            note=f"topup {amount} credits (ref={purchase_ref})",
        )
        await self.db.flush()

    async def apply_monthly_grant(
        self,
        account_id: uuid.UUID,
        grant_amount: int,
        cycle_ref: str,
    ) -> None:
        """
        Applies a monthly subscription credit grant.
        Expires any remaining monthly balance and sets the new balance.
        """
        wallet = await self.get_wallet(account_id)

        if wallet.monthly_balance > 0:
            expired = wallet.monthly_balance
            await self._write_credit_transaction(
                account_id=account_id,
                wallet_id=wallet.wallet_id,
                task_id=None,
                txn_type=TransactionType.subscription_expiry,
                action_type="subscription_expiry",
                amount=-expired,
                balance_after_monthly=0,
                balance_after_topup=wallet.topup_balance,
                reference_id=cycle_ref,
                note=f"subscription expiry {expired} credits (cycle={cycle_ref})",
            )

        wallet.monthly_balance = grant_amount
        wallet.monthly_grant = grant_amount
        wallet.reset_at = datetime.now(timezone.utc)
        wallet.version += 1

        await self._write_credit_transaction(
            account_id=account_id,
            wallet_id=wallet.wallet_id,
            task_id=None,
            txn_type=TransactionType.subscription_grant,
            action_type="subscription_grant",
            amount=grant_amount,
            balance_after_monthly=wallet.monthly_balance,
            balance_after_topup=wallet.topup_balance,
            reference_id=cycle_ref,
            note=f"subscription grant {grant_amount} credits (cycle={cycle_ref})",
        )
        await self.db.flush()

    # ─── Internal helpers ──────────────────────────────────────────────────────

    async def _get_account_id(self, wallet_id: uuid.UUID) -> uuid.UUID:
        result = await self.db.execute(
            select(Wallet.account_id).where(Wallet.wallet_id == wallet_id)
        )
        return result.scalar_one()

    async def _write_credit_transaction(
        self,
        account_id: uuid.UUID,
        wallet_id: uuid.UUID | None,
        task_id: str | None,
        txn_type: TransactionType,
        amount: int,
        action_type: str | None = None,
        balance_after_monthly: int | None = None,
        balance_after_topup: int | None = None,
        reference_id: str | None = None,
        note: str | None = None,
    ) -> CreditTransaction:
        from sqlalchemy import update as sa_update
        from app.models.accounts import Account
        await self.db.execute(
            sa_update(Account)
            .where(Account.account_id == account_id)
            .values(balance_credits=Account.balance_credits + amount)
        )
        txn = CreditTransaction(
            account_id=account_id,
            wallet_id=wallet_id,
            task_id=task_id,
            type=txn_type,
            action_type=action_type or txn_type.value,
            amount=amount,
            balance_after_monthly=balance_after_monthly,
            balance_after_topup=balance_after_topup,
            reference_id=reference_id,
            note=note,
        )
        self.db.add(txn)
        return txn
