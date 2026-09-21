"""
Talos Cloud — Phase 10b User Dashboard & Cross-Account Isolation Tests.

Tests:
  1. Profile & Balance scoping: User sees own balance, subscription, and top-up credits.
  2. Cross-account isolation: User A CANNOT access User B's data under any endpoint,
     even if supplying User B's account_id in query params, body fields, or headers.
  3. Provider field leak prevention: 'provider' field NEVER appears anywhere in
     any user dashboard response payload.
  4. Buy credits: Initiates Razorpay / Stripe top-up order for session account.
"""

import json
import os
import uuid
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models.accounts  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.ledger  # noqa: F401
import app.models.margin_snapshot  # noqa: F401
import app.models.pricing  # noqa: F401
from app.database import Base, get_db
from app.main import app
from app.models.accounts import Account
from app.models.ledger import PricingEvent
from app.services import identity_service, ledger_service


@pytest_asyncio.fixture(scope="module")
async def test_engine():
    from sqlalchemy.pool import NullPool, StaticPool
    db_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    if db_url.startswith("sqlite"):
        engine = create_async_engine(
            db_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False,
        )
    else:
        engine = create_async_engine(db_url, poolclass=NullPool, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine):
    Session = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session


@pytest.fixture
def client(test_engine):
    Session = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_db():
        async with Session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _assert_no_provider_field(response_body: bytes, context: str = ""):
    raw = response_body.decode("utf-8", errors="replace")
    assert '"provider"' not in raw, f"CRITICAL: 'provider' field found in {context}: {raw[:500]}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    def _check(obj, path=""):
        if isinstance(obj, dict):
            assert "provider" not in obj, f"'provider' found at {path} in {context}"
            for k, v in obj.items():
                _check(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _check(item, f"{path}[{i}]")

    _check(data)


class TestUserDashboardIsolation:
    @pytest.mark.asyncio
    async def test_cross_account_isolation_on_all_dashboard_routes(self, db_session, client):
        """
        User A creates session. User B has separate data.
        User A passes User B's account_id via query, header, or body.
        Assert User A receives ONLY User A's data in all endpoints.
        """
        # Create User A
        acc_a = Account(
            account_id=uuid.uuid4(),
            email=f"user_a_{uuid.uuid4().hex[:6]}@example.com",
            role="user",
            balance_credits=0,
            subscription_tier="pro",
        )
        db_session.add(acc_a)
        await db_session.flush()
        await ledger_service.grant_subscription_credits(
            db=db_session, account_id=acc_a.account_id, credits=5000, cycle_ref="a1"
        )
        await ledger_service.grant_topup_credits(
            db=db_session, account_id=acc_a.account_id, credits=1000, purchase_ref="a_top"
        )

        # Create User B with distinct balance and tasks
        acc_b = Account(
            account_id=uuid.uuid4(),
            email=f"user_b_{uuid.uuid4().hex[:6]}@example.com",
            role="user",
            balance_credits=0,
            subscription_tier="enterprise",
        )
        db_session.add(acc_b)
        await db_session.flush()
        await ledger_service.grant_subscription_credits(
            db=db_session, account_id=acc_b.account_id, credits=99000, cycle_ref="b1"
        )

        # Add pricing events for User B
        pe_b = PricingEvent(
            event_id=uuid.uuid4(),
            account_id=acc_b.account_id,
            task_id="secret_task_user_b",
            capability_id="reasoning_model",
            provider="anthropic",  # internal only
            actual_units=500,
            credits_charged=50,
            pricing_version="v1",
        )
        db_session.add(pe_b)
        await db_session.commit()

        # Generate User A session
        token_a = identity_service.create_web_session(acc_a)
        headers_a = {"Authorization": f"Bearer {token_a}"}

        # 1. GET /dashboard/me
        resp_me = client.get(
            f"/dashboard/me?account_id={acc_b.account_id}",
            headers={**headers_a, "X-Account-ID": str(acc_b.account_id)},
        )
        assert resp_me.status_code == 200
        data_me = resp_me.json()
        assert data_me["account_id"] == str(acc_a.account_id)
        assert data_me["email"] == acc_a.email
        assert data_me["subscription_tier"] == "pro"  # not User B's enterprise
        assert data_me["total_balance"] == 6000
        _assert_no_provider_field(resp_me.content, "GET /dashboard/me")

        # 2. GET /dashboard/balance
        resp_bal = client.get(
            f"/dashboard/balance?account_id={acc_b.account_id}",
            headers=headers_a,
        )
        assert resp_bal.status_code == 200
        data_bal = resp_bal.json()
        assert data_bal["subscription_credits"] == 5000  # not User B's 99000
        assert data_bal["topup_credits"] == 1000
        _assert_no_provider_field(resp_bal.content, "GET /dashboard/balance")

        # 3. GET /dashboard/usage
        resp_usage = client.get(
            f"/dashboard/usage?account_id={acc_b.account_id}",
            headers=headers_a,
        )
        assert resp_usage.status_code == 200
        data_usage = resp_usage.json()
        assert data_usage["total_credits_charged"] == 0  # User A has 0, User B has 50
        _assert_no_provider_field(resp_usage.content, "GET /dashboard/usage")

        # 4. GET /dashboard/tasks
        resp_tasks = client.get(
            f"/dashboard/tasks?account_id={acc_b.account_id}",
            headers=headers_a,
        )
        assert resp_tasks.status_code == 200
        data_tasks = resp_tasks.json()
        task_ids = [t["task_id"] for t in data_tasks["tasks"]]
        assert "secret_task_user_b" not in task_ids  # User B's task is invisible to User A
        _assert_no_provider_field(resp_tasks.content, "GET /dashboard/tasks")

        # 5. GET /dashboard/tasks/{task_id}/cost for User B's task
        resp_cost = client.get(
            "/dashboard/tasks/secret_task_user_b/cost",
            headers=headers_a,
        )
        assert resp_cost.status_code == 200
        data_cost = resp_cost.json()
        assert data_cost["total_credits_spent"] == 0
        assert len(data_cost["events"]) == 0
        _assert_no_provider_field(resp_cost.content, "GET /dashboard/tasks/{id}/cost")

    @pytest.mark.asyncio
    async def test_buy_credits_endpoint(self, db_session, client):
        """User can initiate top-up purchase via Razorpay or Stripe."""
        acc = Account(
            account_id=uuid.uuid4(),
            email=f"buyer_{uuid.uuid4().hex[:6]}@example.com",
            role="user",
            balance_credits=100,
        )
        db_session.add(acc)
        await db_session.commit()

        token = identity_service.create_web_session(acc)
        headers = {"Authorization": f"Bearer {token}"}

        # Razorpay purchase
        resp_rzp = client.post(
            "/dashboard/buy-credits",
            json={"gateway": "razorpay", "tier": "starter"},
            headers=headers,
        )
        assert resp_rzp.status_code == 200
        data_rzp = resp_rzp.json()
        assert data_rzp["gateway"] == "razorpay"
        assert data_rzp["amount"] == 49900
        assert data_rzp["credits"] == 5000

        # Stripe purchase
        resp_strp = client.post(
            "/dashboard/buy-credits",
            json={"gateway": "stripe", "tier": "growth"},
            headers=headers,
        )
        assert resp_strp.status_code == 200
        data_strp = resp_strp.json()
        assert data_strp["gateway"] == "stripe"
        assert data_strp["credits"] == 20000
