"""
Talos Cloud — Reservation Recovery Worker Tests.
"""

import uuid
import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import select

from app.models.accounts import Account
from app.models.wallet import Wallet, CreditReservation, ReservationStatus
from app.services.wallet_engine import WalletEngine
from app.services.recovery_worker import ReservationRecoveryWorker


@pytest.mark.asyncio
async def test_recovery_worker_cleanup_stale_reservation(db_session):
    user_id = uuid.uuid4()
    account = Account(account_id=user_id, email=f"recov_{user_id}@example.com")
    db_session.add(account)
    await db_session.flush()

    engine = WalletEngine(db_session)
    wallet = await engine.create_wallet(account_id=user_id, initial_monthly=20, initial_topup=0)

    # Create reservation
    resv = await engine.reserve(
        account_id=user_id,
        task_id="stale_task",
        amount=5,
        idempotency_key="stale_req",
    )

    # Manually expire reservation
    resv.expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    await db_session.commit()

    # Run recovery worker
    cleaned = await ReservationRecoveryWorker.cleanup_stale_reservations(db_session)
    assert cleaned == 1

    # Verify reservation is EXPIRED and balance restored
    await db_session.refresh(resv)
    assert resv.status == ReservationStatus.EXPIRED

    res_final = await db_session.execute(select(Wallet).where(Wallet.account_id == user_id))
    wallet_final = res_final.scalar_one()
    assert wallet_final.monthly_balance == 20
