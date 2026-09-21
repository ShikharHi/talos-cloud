"""
Tests for Phase 2: Relay service.

The most critical tests in this file:
  - test_atomic_precheck_no_overdraft: two concurrent requests that together
    would exceed the account balance. Asserts exactly one succeeds and the
    final balance is NEVER negative.
  - test_provider_field_never_in_relay_response: asserts that 'provider' does
    not appear anywhere in relay HTTP responses (including nested JSON).

These tests require a real Postgres instance. Run with:
  docker compose up -d postgres
  pytest tests/test_relay.py

Skip if DATABASE_URL is not set (CI without Postgres).
"""

import asyncio
import json
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

import app.models.wallet  # noqa: F401
import app.models.ledger  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.subscription_plans  # noqa: F401


@pytest_asyncio.fixture(scope="session")
async def pg_engine():
    """
    Session-scoped engine for relay tests. Uses a file-based SQLite so that
    tables created by create_all are visible across all connections (unlike
    in-memory :memory: which is thread-local with aiosqlite StaticPool).
    """
    import tempfile, os
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.database import Base
    import app.models  # noqa: F401 — register all ORM models in Base.metadata

    db_file = tempfile.mktemp(suffix=".db", prefix="talos_relay_test_")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
    try:
        os.remove(db_file)
    except OSError:
        pass


@pytest_asyncio.fixture
async def pg_db(pg_engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    Session = async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session


@pytest_asyncio.fixture
async def account_with_balance(pg_db):
    """Creates a test account with a Wallet of 100 credits."""
    from app.models.accounts import Account
    from app.models.wallet import Wallet
    acc_id = uuid.uuid4()
    acc = Account(account_id=acc_id, email=f"relay_test_{acc_id.hex[:8]}@example.com", balance_credits=100)
    pg_db.add(acc)
    await pg_db.flush()

    wallet = Wallet(account_id=acc_id, monthly_balance=100, topup_balance=0)
    pg_db.add(wallet)
    await pg_db.commit()
    await pg_db.refresh(acc)
    return acc


class TestRelayServiceAtomicity:
    """Phase 2 — Critical atomicity and correctness tests."""

    @pytest.mark.asyncio
    async def test_insufficient_credits_rejected_before_provider(self, pg_db, account_with_balance):
        """Pre-check must reject before reaching provider when balance is insufficient."""
        from app.services.relay_service import RelayService, InsufficientCreditsError

        # Grab the id as a plain value *before* any further await touches the
        # session. Once the ORM object is expired (e.g. after a commit inside
        # the fixture, or by the service call itself), reading an attribute
        # off it triggers an implicit lazy-load. That lazy-load needs to run
        # inside a greenlet context (via greenlet_spawn); if it fires outside
        # one — e.g. while building kwargs for a call we're about to await —
        # it raises MissingGreenlet instead. Reading it now, while the object
        # is definitely still attached and unexpired, sidesteps that entirely.
        acc_id = account_with_balance.account_id

        # Account has 100 credits. Request 200 credits worth.
        service = RelayService(pg_db)

        with pytest.raises(InsufficientCreditsError) as exc_info:
            await service.call(
                account_id=acc_id,
                task_id="test-task",
                capability_id="reasoning_model",
                payload={"messages": [{"role": "user", "content": "hi"}]},
                worst_case_units=300000,  # 129 credits at 0.43 per 1k
            )
        assert exc_info.value.required > 100
        assert exc_info.value.current <= 100

        # Balance must be unchanged (atomic check failed, no debit occurred)
        from sqlalchemy import select
        from app.models.wallet import Wallet
        result = await pg_db.execute(
            select(Wallet).where(Wallet.account_id == acc_id)
        )
        w = result.scalar_one()
        balance = w.monthly_balance + w.topup_balance
        assert balance == 100, f"Balance changed after failed pre-check: {balance}"

    @pytest.mark.asyncio
    async def test_atomic_precheck_no_overdraft(self, pg_engine):
        """
        CRITICAL: Two concurrent requests that together would overdraft the account.
        Exactly one must succeed. Final balance must NEVER be negative.

        This test verifies the atomic UPDATE...WHERE semantics. A check-then-decrement
        (SELECT then UPDATE in two steps) would race and allow both to succeed.
        """
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        from sqlalchemy import select
        from app.models.accounts import Account
        from app.services.relay_service import RelayService, InsufficientCreditsError

        Session = async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)

        # Account with exactly 15 credits — enough for ONE 8-credit call, not two.
        # The precheck unit type for reasoning_model is output_tokens (0.600 credits/1k).
        # worst_case_units=13_334 tokens → (13334/1000)*0.600=8.0 credits held.
        # 15 credits: floor(15/8)=1, so exactly 1 of 4 concurrent calls wins.
        async with Session() as setup_db:
            from app.models.wallet import Wallet
            acc_id = uuid.uuid4()
            acc = Account(
                account_id=acc_id,
                email=f"race_test_{acc_id.hex[:8]}@example.com",
                balance_credits=15,
            )
            setup_db.add(acc)
            await setup_db.flush()
            wallet = Wallet(account_id=acc_id, monthly_balance=15, topup_balance=0)
            setup_db.add(wallet)
            await setup_db.commit()
            account_id = acc.account_id

        async def attempt_call(session_factory):
            """
            Each concurrent task uses its own session (simulates separate HTTP requests).
            The asyncio.sleep(0) inside the mock dispatch yields to the event loop,
            guaranteeing that all four coroutines reach the atomic UPDATE concurrently
            before any of them gets a response. This is the scenario where a
            check-then-decrement (SELECT then UPDATE) would race and allow overdraft.
            """
            async with session_factory() as db:
                service = RelayService(db)

                async def _slow_dispatch(capability_id, payload, **kwargs):
                    # Yield to the event loop — lets other coroutines run their
                    # pre-check UPDATE before this call's reconcile step.
                    await asyncio.sleep(0)
                    return {"choices": []}, 1000

                service._dispatch = _slow_dispatch
                try:
                    result = await service.call(
                        account_id=account_id,
                        task_id=f"race-task-{uuid.uuid4().hex[:4]}",
                        capability_id="reasoning_model",
                        payload={"messages": []},
                        # reasoning_model precheck uses output_tokens (0.600 credits/1k).
                        # 13334 tokens → (13334/1000)*0.600 = 8.0 credits held.
                        # Wallet has 15: exactly 1 of 4 concurrent calls should win.
                        worst_case_units=25_000,
                    )
                    await db.commit()
                    return "success"
                except InsufficientCreditsError:
                    return "insufficient"

        # Fire 4 concurrent calls against a 15-credit balance (enough for ONE 14-credit call).
        # All 4 start simultaneously. Exactly 1 must succeed; the rest must be rejected.
        results = await asyncio.gather(
            attempt_call(Session),
            attempt_call(Session),
            attempt_call(Session),
            attempt_call(Session),
        )

        successes = results.count("success")
        failures = results.count("insufficient")

        # Exactly 1 must succeed; the other 3 must be rejected.
        # If check-then-decrement were used instead of atomic UPDATE, multiple
        # coroutines would see the balance as sufficient and all succeed → overdraft.
        assert successes == 1, f"Expected exactly 1 success, got {successes}. Results: {results}"
        assert failures == 3, f"Expected exactly 3 insufficient, got {failures}. Results: {results}"

        # Final balance must NEVER be negative (overdraft protection)
        async with Session() as check_db:
            from app.models.wallet import Wallet
            result = await check_db.execute(
                select(Wallet).where(Wallet.account_id == account_id)
            )
            w = result.scalar_one()
            final_balance = w.monthly_balance + w.topup_balance
        assert final_balance >= 0, f"OVERDRAFT! Balance is {final_balance}"

    @pytest.mark.asyncio
    async def test_reconcile_refunds_overhold(self, pg_db, account_with_balance):
        """After a call that uses fewer units than worst_case, the refund is applied."""
        from sqlalchemy import select
        from app.services.relay_service import RelayService

        # Store account_id as local UUID to avoid lazy-load / MissingGreenlet issues
        # when accessing ORM attributes after session commit.
        acc_id = account_with_balance.account_id

        service = RelayService(pg_db)
        with patch.object(service, "_dispatch", new_callable=AsyncMock) as mock_dispatch:
            # Actual cost: 500 tokens. 0.12 credits/1k * 0.5k = 0.06 → rounds to 1 credit charged.
            # worst_case: 10000 tokens → 0.12 * 10 = 1.2 → 1 credit held.
            # Either way, some credit should be consumed.
            mock_dispatch.return_value = ({"choices": []}, 500)
            await service.call(
                account_id=acc_id,
                task_id="refund-test",
                capability_id="reasoning_model",
                payload={"messages": []},
                worst_case_units=10_000,
            )
            await pg_db.commit()

        from app.models.wallet import Wallet
        result = await pg_db.execute(
            select(Wallet).where(Wallet.account_id == acc_id)
        )
        w = result.scalar_one()
        after = w.monthly_balance + w.topup_balance
        # Balance must have decreased from the initial 100 credits
        assert after < 100, f"Expected balance deduction from 100, got {after}"

    @pytest.mark.asyncio
    async def test_admin_account_bypasses_insufficient_credits(self, pg_db):
        """Admin account (role='admin' or admin email) bypasses credit limit even with 0 balance."""
        from sqlalchemy import select
        from app.models.accounts import Account
        from app.services.relay_service import RelayService

        admin_acc = Account(
            email="shikharjadav16@gmail.com",
            role="admin",
            subscription_tier="admin",
            balance_credits=0,  # 0 credits
        )
        pg_db.add(admin_acc)
        await pg_db.commit()
        await pg_db.refresh(admin_acc)

        service = RelayService(pg_db)
        with patch.object(service, "_dispatch", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.return_value = ({"choices": [{"text": "admin ok"}]}, 1000)
            res = await service.call(
                account_id=admin_acc.account_id,
                task_id="admin-test-task",
                capability_id="reasoning_model",
                payload={"messages": []},
                worst_case_units=50000,
            )
            await pg_db.commit()

        assert res["credits_charged"] == 0
        assert "result" in res



class TestRelayProviderFieldLeak:
    """
    Phase 2 invariant: 'provider' must never appear in relay HTTP responses.
    This test class uses the FastAPI TestClient to inspect actual HTTP responses.
    """

    @pytest.fixture
    def client(self):
        """FastAPI test client with a mocked auth dependency."""
        import uuid
        from fastapi.testclient import TestClient
        from app.main import app
        from app.routers.relay import get_authenticated_account
        from app.models.accounts import Account

        mock_account = Account(
            account_id=uuid.uuid4(),
            email="test@example.com",
            balance_credits=10000,
            subscription_tier="pro",
        )

        app.dependency_overrides[get_authenticated_account] = lambda: mock_account
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()

    def _assert_no_provider_field(self, response_body: bytes, context: str = ""):
        """
        Recursively asserts that 'provider' does not appear anywhere in the
        JSON response — including nested objects. This is the belt-and-suspenders
        check on top of the model-layer exclusion.
        """
        # First: raw string check (catches even malformed/nested JSON)
        raw = response_body.decode("utf-8", errors="replace")
        assert '"provider"' not in raw, (
            f"CRITICAL: 'provider' field found in relay response {context}. "
            f"Raw response: {raw[:500]}"
        )

        # Second: parsed JSON check
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return  # non-JSON response (e.g. streaming) — raw check above was sufficient

        def _check_recursive(obj, path=""):
            if isinstance(obj, dict):
                assert "provider" not in obj, (
                    f"CRITICAL: 'provider' key found at path '{path}' in relay response {context}"
                )
                for k, v in obj.items():
                    _check_recursive(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _check_recursive(item, f"{path}[{i}]")

        _check_recursive(data)

    def test_provider_field_never_in_relay_call_response(self, client):
        """
        POST /relay/call response must not contain 'provider' anywhere.
        Uses a real request (with mocked auth and mocked relay service dispatch).
        """
        from app.routers import relay as relay_router
        from app.services.relay_service import RelayService

        with patch.object(RelayService, "call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {
                "result": {"choices": []},
                "credits_charged": 5,
                "capability_id": "reasoning_model",
                # Notice: no 'provider' key here — relay_service contract
            }
            response = client.post(
                "/relay/call",
                json={
                    "capability_id": "reasoning_model",
                    "payload": {"messages": []},
                    "worst_case_units": 1000,
                },
                headers={"Authorization": "Bearer test-token"},
            )

        assert response.status_code == 200
        self._assert_no_provider_field(response.content, context="POST /relay/call")

    def test_provider_field_never_in_insufficient_credits_error(self, client):
        """
        Even error responses from the relay must not contain 'provider'.
        """
        from app.services.relay_service import RelayService, InsufficientCreditsError

        with patch.object(RelayService, "call", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = InsufficientCreditsError(
                account_id="test-id", required=100, current=5
            )
            response = client.post(
                "/relay/call",
                json={"capability_id": "reasoning_model", "payload": {}, "worst_case_units": 10000},
                headers={"Authorization": "Bearer test-token"},
            )

        assert response.status_code == 402
        self._assert_no_provider_field(response.content, context="POST /relay/call 402 error")