"""
Tests for Phase 3: Credit Ledger.

Tests:
  - subscription grant writes correct transaction and updates balance
  - topup grant writes correct transaction and updates balance
  - subscription expiry writes negative transaction for unused sub credits only
  - top-up credits are NEVER expired
  - ledger auditability: SUM(transactions.amount) == accounts.balance_credits always
  - balance breakdown correctly attributes subscription vs topup
  - no credit-to-currency conversion appears in any response (grep check)
  - margin_monitor never creates or modifies a pricing_version row
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import os


@pytest_asyncio.fixture(scope="module")
async def engine():
    from app.database import Base
    import app.models.accounts  # noqa
    import app.models.ledger  # noqa
    import app.models.pricing  # noqa
    import app.models.billing  # noqa
    from sqlalchemy.pool import NullPool, StaticPool

    db_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    if db_url.startswith("sqlite"):
        eng = create_async_engine(
            db_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False,
        )
    else:
        eng = create_async_engine(db_url, poolclass=NullPool, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def account(db):
    from app.models.accounts import Account
    acc = Account(email=f"ledger_{uuid.uuid4().hex[:8]}@example.com", balance_credits=0)
    db.add(acc)
    await db.commit()
    await db.refresh(acc)
    return acc


class TestLedgerService:

    @pytest.mark.asyncio
    async def test_subscription_grant_updates_balance(self, db, account):
        """Grant subscription credits → balance increases, transaction written."""
        from app.services.ledger_service import grant_subscription_credits
        from app.models.accounts import Account
        from sqlalchemy import select

        txn = await grant_subscription_credits(
            db, account_id=account.account_id, credits=100, cycle_ref="2026-08"
        )
        await db.commit()

        assert txn.amount == 100
        assert txn.type.value == "subscription_grant"

        result = await db.execute(
            select(Account.balance_credits).where(Account.account_id == account.account_id)
        )
        assert result.scalar_one() == 100

    @pytest.mark.asyncio
    async def test_topup_grant_updates_balance(self, db, account):
        """Grant top-up credits → balance increases, correct transaction type."""
        from app.services.ledger_service import grant_topup_credits
        from app.models.accounts import Account
        from sqlalchemy import select

        txn = await grant_topup_credits(
            db, account_id=account.account_id, credits=50, purchase_ref="stripe-pi-test"
        )
        await db.commit()

        assert txn.amount == 50
        assert txn.type.value == "topup"

        result = await db.execute(
            select(Account.balance_credits).where(Account.account_id == account.account_id)
        )
        # account had 100 from sub grant in previous test (same fixture scope for db)
        # We use a fresh account fixture per test so balance starts at 0+50=50
        assert result.scalar_one() == 50

    @pytest.mark.asyncio
    async def test_ledger_auditability_after_grants(self, db):
        """SUM(transactions.amount) must always equal accounts.balance_credits."""
        from app.models.accounts import Account
        from app.services.ledger_service import (
            grant_subscription_credits,
            grant_topup_credits,
            verify_ledger_auditability,
        )

        # Fresh account for this test
        acc = Account(email=f"audit_{uuid.uuid4().hex[:8]}@example.com", balance_credits=0)
        db.add(acc)
        await db.flush()

        await grant_subscription_credits(db, acc.account_id, 200, "2026-08")
        await grant_topup_credits(db, acc.account_id, 75, "stripe-001")
        await db.commit()

        is_consistent = await verify_ledger_auditability(db, acc.account_id)
        assert is_consistent, "Ledger sum does not match live balance"

    @pytest.mark.asyncio
    async def test_subscription_expiry_only_expires_sub_credits(self, db):
        """
        Expiry transaction must only cover unused subscription credits.
        Top-up credits must NEVER be expired.
        """
        from sqlalchemy import select
        from app.models.accounts import Account
        from app.models.ledger import CreditTransaction, TransactionType
        from app.services.ledger_service import (
            grant_subscription_credits,
            grant_topup_credits,
            expire_subscription_credits,
            verify_ledger_auditability,
        )

        acc = Account(email=f"expiry_{uuid.uuid4().hex[:8]}@example.com", balance_credits=0)
        db.add(acc)
        await db.flush()

        # Grant 100 sub + 50 topup
        await grant_subscription_credits(db, acc.account_id, 100, "2026-08")
        await grant_topup_credits(db, acc.account_id, 50, "stripe-002")
        await db.commit()

        # Expire all unused subscription credits
        expiry_txn = await expire_subscription_credits(db, acc.account_id, "2026-08")
        await db.commit()

        assert expiry_txn is not None
        assert expiry_txn.type == TransactionType.subscription_expiry
        # Only sub credits expired (100), not topup (50)
        assert expiry_txn.amount == -100

        # Balance should now be just the topup credits (50)
        result = await db.execute(
            select(Account.balance_credits).where(Account.account_id == acc.account_id)
        )
        remaining = result.scalar_one()
        assert remaining == 50, f"Expected 50 (topup only), got {remaining}"

        # Ledger must still be auditable
        assert await verify_ledger_auditability(db, acc.account_id)

    @pytest.mark.asyncio
    async def test_topup_never_expires(self, db):
        """
        Top-up credits must not be expired by expire_subscription_credits.
        Calling expiry on an account with only topup credits must return None.
        """
        from app.models.accounts import Account
        from app.services.ledger_service import grant_topup_credits, expire_subscription_credits

        acc = Account(email=f"topup_only_{uuid.uuid4().hex[:8]}@example.com", balance_credits=0)
        db.add(acc)
        await db.flush()

        await grant_topup_credits(db, acc.account_id, 200, "stripe-003")
        await db.commit()

        # No subscription credits → expiry call returns None
        result = await expire_subscription_credits(db, acc.account_id, "2026-08")
        assert result is None, "expiry_transaction should be None when no sub credits exist"

    @pytest.mark.asyncio
    async def test_balance_breakdown_separates_sub_and_topup(self, db):
        """
        get_balance_breakdown returns subscription_credits, topup_credits, total_credits.
        No currency field in the result.
        """
        from app.models.accounts import Account
        from app.services.ledger_service import grant_subscription_credits, grant_topup_credits, get_balance_breakdown

        acc = Account(email=f"breakdown_{uuid.uuid4().hex[:8]}@example.com", balance_credits=0)
        db.add(acc)
        await db.flush()

        await grant_subscription_credits(db, acc.account_id, 80, "2026-08")
        await grant_topup_credits(db, acc.account_id, 30, "stripe-004")
        await db.commit()

        breakdown = await get_balance_breakdown(db, acc.account_id)

        assert breakdown["total_credits"] == 110
        assert "subscription_credits" in breakdown
        assert "topup_credits" in breakdown
        # No currency field ever
        assert "usd" not in str(breakdown).lower()
        assert "currency" not in str(breakdown).lower()
        assert "1 credit" not in str(breakdown).lower()


class TestMarginMonitorInvariant:
    """
    INVARIANT: margin_monitor.py must NEVER create or modify a pricing_version row.
    """

    @pytest.mark.asyncio
    async def test_monitor_never_creates_pricing_version(self, db):
        """
        After running the margin check, the pricing_versions table must be unchanged.
        """
        from sqlalchemy import select, func
        from app.models.pricing import PricingVersion
        from app.services.margin_monitor import run_margin_check

        # Count before
        result_before = await db.execute(select(func.count()).select_from(PricingVersion))
        count_before = result_before.scalar_one()

        # Run the margin check
        await run_margin_check(db)

        # Count after — must be identical
        result_after = await db.execute(select(func.count()).select_from(PricingVersion))
        count_after = result_after.scalar_one()

        assert count_after == count_before, (
            f"CRITICAL INVARIANT VIOLATION: margin_monitor created {count_after - count_before} "
            f"pricing_version row(s). The monitor must NEVER publish pricing versions."
        )

    def test_no_credit_to_currency_in_ledger_service(self):
        """
        Grep check: no credit-to-currency conversion in ledger_service source.
        Forbidden patterns: '1 credit =', 'usd_per_credit', 'credits_to_usd', 'credit_to_currency'
        """
        import inspect
        from app.services import ledger_service

        source = inspect.getsource(ledger_service)
        forbidden = [
            "1 credit =",
            "usd_per_credit",
            "credits_to_usd",
            "credit_to_currency",
        ]
        for pattern in forbidden:
            assert pattern not in source, (
                f"Credit-to-currency conversion '{pattern}' found in ledger_service.py. "
                "This must never be exposed publicly."
            )
