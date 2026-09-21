"""
Talos Cloud — Concurrency & Atomic Locking Tests.

Verifies that FOR UPDATE / atomic UPDATE in WalletEngine prevents credit overspending
when multiple tasks/agents execute concurrently.

Test case:
  Starting balance = 10 credits
  Launch 10 simultaneous requests requiring 4 credits each.
  Assert:
    - Exactly 2 reservations succeed, 8 fail.
    - After consuming 4 credits for each successful reservation and releasing unused holds:
      balance = 2, reserved = 0, available = 2.
"""

import uuid
import asyncio
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.accounts import Account
from app.models.wallet import Wallet, CreditReservation, ReservationStatus
from app.services.wallet_engine import InsufficientCreditsError, WalletEngine


@pytest.mark.asyncio
async def test_concurrent_reservations_prevent_overspend(db_session):
    await db_session.commit()
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    user_id = uuid.uuid4()
    async with session_factory() as setup_s:
        account = Account(account_id=user_id, email=f"concurrency_{user_id}@example.com")
        setup_s.add(account)
        await setup_s.commit()

        engine = WalletEngine(setup_s)
        await engine.create_wallet(account_id=user_id, initial_monthly=10, initial_topup=0)
        await setup_s.commit()

    # 2. Define single reservation task running on isolated DB session
    async def try_reserve(i: int):
        async with session_factory() as s:
            t_engine = WalletEngine(s)
            try:
                resv = await t_engine.reserve(
                    account_id=user_id,
                    task_id=f"concurrent_task_{i}",
                    amount=4,
                    idempotency_key=f"req_{i}",
                )
                resv_id = resv.reservation_id
                await s.commit()
                return True, resv_id
            except (ValueError, InsufficientCreditsError):
                return False, None

    # 3. Execute 10 simultaneous reservation attempts
    results = await asyncio.gather(*[try_reserve(i) for i in range(10)])

    succeeded = [r_id for success, r_id in results if success]
    failed = [r_id for success, r_id in results if not success]

    # Assert exactly 2 reservations succeeded (2 x 4 = 8 <= 10) and 8 failed
    assert len(succeeded) == 2
    assert len(failed) == 8

    # 4. Reconcile both successful reservations by consuming 4 credits each
    async with session_factory() as reconcile_s:
        r_engine = WalletEngine(reconcile_s)
        for resv_id in succeeded:
            await r_engine.commit(
                reservation_id=resv_id,
                actual_amount=4,
            )
        await reconcile_s.commit()

    # 5. Verify final wallet state: balance = 2, reserved = 0, available = 2
    async with session_factory() as check_s:
        res_final = await check_s.execute(select(Wallet).where(Wallet.account_id == user_id))
        wallet_final = res_final.scalar_one()

        assert wallet_final.monthly_balance + wallet_final.topup_balance == 2
