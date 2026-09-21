"""
Talos Cloud — SubscriptionService Unit & Idempotency Tests.

Tests:
  - test_enroll_creates_subscription_and_wallet_grant
  - test_monthly_reset_idempotence
  - test_plan_capability_access_control
"""

import uuid
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import Account
from app.models.subscription_plans import SubscriptionPlan
from app.services.subscription_service import SubscriptionService
from app.services.wallet_engine import WalletEngine


@pytest.fixture
async def seeded_plans(db_session: AsyncSession):
    """Seed subscription_plans table for testing."""
    plans = [
        SubscriptionPlan(name="free", price_usd=0, monthly_credits=50, reset_period_days=30),
        SubscriptionPlan(name="plus", price_usd=19, monthly_credits=220, reset_period_days=30),
        SubscriptionPlan(name="pro", price_usd=29, monthly_credits=350, reset_period_days=30),
        SubscriptionPlan(name="pro_plus", price_usd=49, monthly_credits=600, reset_period_days=30),
        SubscriptionPlan(name="ultra", price_usd=99, monthly_credits=1300, reset_period_days=30),
    ]
    for p in plans:
        db_session.add(p)
    await db_session.flush()
    return plans


@pytest.mark.asyncio
async def test_enroll_creates_subscription_and_grant(db_session: AsyncSession, seeded_plans):
    svc = SubscriptionService(db_session)
    wallet_eng = WalletEngine(db_session)

    account_id = uuid.uuid4()
    account = Account(account_id=account_id, email=f"sub_test_{account_id}@example.com")
    db_session.add(account)
    await db_session.flush()

    sub = await svc.enroll(account_id, "plus")
    assert sub.status == "active"

    # Wallet should be created with 220 monthly credits
    bal = await wallet_eng.get_balance(account_id)
    assert bal["monthly_credits"] == 220
    assert bal["topup_credits"] == 0


@pytest.mark.asyncio
async def test_monthly_reset_idempotence(db_session: AsyncSession, seeded_plans):
    svc = SubscriptionService(db_session)
    wallet_eng = WalletEngine(db_session)

    account_id = uuid.uuid4()
    account = Account(account_id=account_id, email=f"idemp_sub_{account_id}@example.com")
    db_session.add(account)
    await db_session.flush()

    sub = await svc.enroll(account_id, "pro")

    # Manually simulate period expiry by rewinding next_reset_at to the past
    past_time = datetime.now(timezone.utc) - timedelta(days=1)
    sub.next_reset_at = past_time
    await db_session.flush()

    # First reset call → performs reset (returns True)
    was_reset = await svc.monthly_reset(sub.id)
    assert was_reset is True

    # Check balance reset to 350
    bal1 = await wallet_eng.get_balance(account_id)
    assert bal1["monthly_credits"] == 350

    # Second reset call immediately after → must return False (idempotent no-op)
    was_reset_again = await svc.monthly_reset(sub.id)
    assert was_reset_again is False

    # Balance must NOT be doubled
    bal2 = await wallet_eng.get_balance(account_id)
    assert bal2["monthly_credits"] == 350


@pytest.mark.asyncio
async def test_plan_capability_access_control(db_session: AsyncSession, seeded_plans):
    svc = SubscriptionService(db_session)

    account_id = uuid.uuid4()
    account = Account(account_id=account_id, email=f"access_test_{account_id}@example.com")
    db_session.add(account)
    await db_session.flush()

    # Free plan enrollment
    await svc.enroll(account_id, "free")

    # Free plan permits fast_model, web_search, code_model
    assert await svc.get_plan_access(account_id, "fast_model") is True
    assert await svc.get_plan_access(account_id, "web_search") is True
    assert await svc.get_plan_access(account_id, "code_model") is True

    # Free plan denies reasoning_model and image_gen
    assert await svc.get_plan_access(account_id, "reasoning_model") is False
    assert await svc.get_plan_access(account_id, "image_gen") is False
