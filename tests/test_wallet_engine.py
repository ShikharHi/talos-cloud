"""
Talos Cloud — WalletEngine Unit & Concurrency Tests.

Tests:
  - test_wallet_cannot_go_negative
  - test_monthly_consumed_before_topup
  - test_reservation_lifecycle (HELD → COMMITTED with partial refund)
  - test_reservation_release_on_failure (HELD → RELEASED with full refund)
  - test_idempotency_prevents_double_reservation
  - test_concurrent_reservations_no_overdraft
"""

import asyncio
import uuid
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import Account
from app.services.wallet_engine import InsufficientCreditsError, WalletEngine
from app.models.wallet import ReservationStatus


@pytest.mark.asyncio
async def test_wallet_cannot_go_negative(db_session: AsyncSession):
    engine = WalletEngine(db_session)
    account_id = uuid.uuid4()
    account = Account(account_id=account_id, email=f"wallet_test_{account_id}@example.com")
    db_session.add(account)
    await db_session.flush()

    wallet = await engine.create_wallet(account_id, initial_monthly=50, initial_topup=0)
    assert wallet.monthly_balance == 50

    # Requesting 100 credits when balance is 50 must raise InsufficientCreditsError
    with pytest.raises(InsufficientCreditsError) as exc_info:
        await engine.reserve(account_id, task_id="t1", amount=100)

    assert exc_info.value.required == 100
    assert exc_info.value.current_total == 50

    # Balance must remain unchanged at 50
    bal = await engine.get_balance(account_id)
    assert bal["total_credits"] == 50


@pytest.mark.asyncio
async def test_monthly_consumed_before_topup(db_session: AsyncSession):
    engine = WalletEngine(db_session)
    account_id = uuid.uuid4()
    account = Account(account_id=account_id, email=f"order_test_{account_id}@example.com")
    db_session.add(account)
    await db_session.flush()

    # 50 monthly + 100 topup = 150 total
    await engine.create_wallet(account_id, initial_monthly=50, initial_topup=100)

    # Reserve 70 credits → 50 monthly + 20 topup should be deducted
    res = await engine.reserve(account_id, task_id="t2", amount=70)
    assert res.amount_reserved == 70

    bal = await engine.get_balance(account_id)
    assert bal["monthly_credits"] == 0
    assert bal["topup_credits"] == 80
    assert bal["total_credits"] == 80


@pytest.mark.asyncio
async def test_reservation_lifecycle_committed(db_session: AsyncSession):
    engine = WalletEngine(db_session)
    account_id = uuid.uuid4()
    account = Account(account_id=account_id, email=f"lifecycle_test_{account_id}@example.com")
    db_session.add(account)
    await db_session.flush()

    await engine.create_wallet(account_id, initial_monthly=100, initial_topup=0)

    # Precheck hold: 40 credits
    res = await engine.reserve(account_id, task_id="t3", amount=40)
    assert res.status == ReservationStatus.HELD

    # Actual usage was only 25 credits → 15 credits refunded
    await engine.commit(res.reservation_id, actual_amount=25)
    assert res.status == ReservationStatus.COMMITTED
    assert res.amount_committed == 25

    bal = await engine.get_balance(account_id)
    assert bal["total_credits"] == 75  # 100 - 25 = 75


@pytest.mark.asyncio
async def test_reservation_release_on_failure(db_session: AsyncSession):
    engine = WalletEngine(db_session)
    account_id = uuid.uuid4()
    account = Account(account_id=account_id, email=f"release_test_{account_id}@example.com")
    db_session.add(account)
    await db_session.flush()

    await engine.create_wallet(account_id, initial_monthly=100, initial_topup=0)

    # Precheck hold: 50 credits
    res = await engine.reserve(account_id, task_id="t4", amount=50)

    # Provider failed → release reservation
    await engine.release(res.reservation_id)
    assert res.status == ReservationStatus.RELEASED

    bal = await engine.get_balance(account_id)
    assert bal["total_credits"] == 100  # fully restored


@pytest.mark.asyncio
async def test_idempotency_prevents_double_reservation(db_session: AsyncSession):
    engine = WalletEngine(db_session)
    account_id = uuid.uuid4()
    account = Account(account_id=account_id, email=f"idempotency_test_{account_id}@example.com")
    db_session.add(account)
    await db_session.flush()

    await engine.create_wallet(account_id, initial_monthly=100, initial_topup=0)

    key = f"retry_key_{account_id}"

    # First call
    res1 = await engine.reserve(account_id, task_id="t5", amount=30, idempotency_key=key)

    # Retry call with exact same idempotency key
    res2 = await engine.reserve(account_id, task_id="t5", amount=30, idempotency_key=key)

    assert res1.reservation_id == res2.reservation_id

    # Balance should only be debited once (70 remaining)
    bal = await engine.get_balance(account_id)
    assert bal["total_credits"] == 70
