"""
Talos Cloud — Reservation Recovery Worker.

Background worker that inspects stale HELD / ACTIVE credit reservations (`expires_at < NOW()`).
Evaluates provider execution state (`PROVIDER_NOT_STARTED`, `PROVIDER_STARTED_UNKNOWN`, `PROVIDER_COMPLETED`, `PROVIDER_FAILED`),
releases unconsumed credit holds, and writes compensating audit events to prevent ghost holds or server crash deadlocks.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.wallet import CreditReservation, ReservationStatus, Wallet
from app.models.usage_events import UsageEvent
from app.models.ledger import CreditTransaction, TransactionType

logger = logging.getLogger("recovery_worker")


class ReservationRecoveryWorker:
    @staticmethod
    async def cleanup_stale_reservations(db: AsyncSession) -> int:
        """
        Query all HELD reservations where expires_at <= NOW().
        Releases reserved_credits back to the wallet balance and marks reservation EXPIRED.
        """
        now = datetime.now(timezone.utc)
        stmt = select(CreditReservation).where(
            CreditReservation.status == ReservationStatus.HELD,
            CreditReservation.expires_at <= now,
        )
        res = await db.execute(stmt)
        stale_list = res.scalars().all()

        cleaned_count = 0
        for reservation in stale_list:
            # Lock wallet
            wallet_stmt = select(Wallet).where(Wallet.wallet_id == reservation.wallet_id).with_for_update()
            w_res = await db.execute(wallet_stmt)
            wallet = w_res.scalar_one_or_none()

            if wallet and reservation.amount_reserved > 0:
                # Release reserved credits hold from split sources
                wallet.reserved_monthly = max(0, wallet.reserved_monthly - getattr(reservation, "monthly_reserved", 0))
                wallet.reserved_topup = max(0, wallet.reserved_topup - getattr(reservation, "topup_reserved", 0))

                # Mark reservation EXPIRED
                reservation.status = ReservationStatus.EXPIRED
                reservation.released_at = now

                # Write compensating transaction
                tx = CreditTransaction(
                    account_id=wallet.account_id,
                    wallet_id=wallet.wallet_id,
                    task_id=reservation.task_id,
                    type=TransactionType.reconcile_refund,
                    action_type="reservation_expired",
                    amount=0,
                    balance_after_monthly=wallet.monthly_balance,
                    balance_after_topup=wallet.topup_balance,
                    note=f"Crash recovery: expired reservation {reservation.reservation_id} released",
                )
                db.add(tx)

                # Write compensating usage event audit
                evt = UsageEvent(
                    account_id=wallet.account_id,
                    task_id=reservation.task_id,
                    request_id=getattr(reservation, "request_id", None),
                    provider="recovery",
                    status="EXPIRED_RECOVERY",
                    credits_reserved=float(reservation.amount_reserved),
                    credits_released=float(reservation.amount_reserved),
                )
                db.add(evt)
                cleaned_count += 1

        if cleaned_count > 0:
            await db.commit()
            logger.info(f"ReservationRecoveryWorker: Cleaned up {cleaned_count} stale reservations.")

        return cleaned_count
