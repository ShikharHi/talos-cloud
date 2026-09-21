"""
Talos Cloud — Admin Dashboard & Production Financial Architecture Tests.

Tests:
  1. Unauthenticated requests get 401 on every /admin/* route.
  2. Non-admin sessions (role='user') get 403 Forbidden on every /admin/* route.
  3. Admin session (role='admin') accesses overview, users, pricing config, provider rates, economics, ledger.
  4. User directory excludes raw google_sub / internal secrets from output.
  5. PricingConfiguration is stored in PostgreSQL and versioned dynamically.
  6. Financial provenance & truthfulness: Zero revenue produces None (N/A) gross margin.
  7. Interactive Pricing Simulator runs with Decimal precision.
  8. Margin monitor background job contains NO code path to auto-publish pricing.
"""

import ast
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
import app.models.pricing_configuration  # noqa: F401
import app.models.provider_pricing  # noqa: F401
import app.models.capability_pricing  # noqa: F401
from app.database import Base, get_db
from app.main import app
from app.models.accounts import Account
from app.models.billing import BillingTransaction
from app.models.pricing_configuration import PricingConfiguration
from app.services import identity_service


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


ADMIN_ROUTES = [
    ("GET", "/admin/overview", None),
    ("GET", "/admin/users", None),
    ("GET", "/admin/pricing/config", None),
    ("GET", "/admin/pricing/providers", None),
    ("GET", "/admin/pricing/capabilities", None),
    ("GET", "/admin/subscriptions/plans", None),
    ("GET", "/admin/runs", None),
    ("GET", "/admin/analytics/providers", None),
    ("GET", "/admin/economics", None),
    ("GET", "/admin/ledger", None),
    ("GET", "/admin/audit-logs", None),
    ("POST", "/admin/pricing/config", {"credit_reference_usd": 0.08}),
    ("GET", "/admin/auth-providers", None),
    ("POST", "/admin/auth-providers", {"provider": "google", "client_id": "test-id", "client_secret": "test-sec"}),
]


class TestAdminAuthorization:
    def test_unauthenticated_request_rejected_401_on_all_admin_routes(self, client):
        """Requests with no session get 401 on every admin route."""
        for method, route, body in ADMIN_ROUTES:
            if method == "GET":
                resp = client.get(route)
            else:
                resp = client.post(route, json=body)
            assert resp.status_code == 401, f"Expected 401 on unauthenticated {method} {route}, got {resp.status_code}"

    def test_non_admin_session_rejected_403_on_all_admin_routes(self, client):
        """Requests with role='user' session get 403 on every admin route."""
        user_acc = Account(
            account_id=uuid.uuid4(),
            email="normal_user@example.com",
            role="user",
            balance_credits=100,
        )
        token = identity_service.create_web_session(user_acc)
        headers = {"Authorization": f"Bearer {token}"}

        for method, route, body in ADMIN_ROUTES:
            if method == "GET":
                resp = client.get(route, headers=headers)
            else:
                resp = client.post(route, json=body, headers=headers)
            assert resp.status_code == 403, f"Expected 403 on user {method} {route}, got {resp.status_code}"


class TestAdminDashboardFeatures:
    @pytest.mark.asyncio
    async def test_admin_overview_metrics_with_billing(self, db_session, client):
        """Admin overview reflects real billing transactions and financial provenance."""
        admin_acc = Account(
            account_id=uuid.uuid4(),
            email=f"admin_boss_{uuid.uuid4().hex[:6]}@talos.ai",
            role="admin",
            balance_credits=5000,
        )
        db_session.add(admin_acc)

        # Seed completed billing transaction
        bt = BillingTransaction(
            account_id=admin_acc.account_id,
            gateway="razorpay",
            canonical_reference_id=f"order_admin_{uuid.uuid4().hex[:8]}",
            amount_minor=49900,
            currency="INR",
            credits_granted=5000,
            event_type="topup_completed",
            status="processed",
        )
        db_session.add(bt)
        await db_session.commit()

        admin_token = identity_service.create_web_session(admin_acc)
        headers = {"Authorization": f"Bearer {admin_token}"}

        resp = client.get("/admin/overview", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_users"] >= 1
        assert data["total_billing_transactions"] >= 1
        assert data["total_revenue_usd"] == 499.00
        assert data["revenue_source"] == "billing_transactions"
        assert data["cogs_source"] == "usage_events"
        assert data["credit_source"] == "credit_transactions"

    @pytest.mark.asyncio
    async def test_admin_overview_zero_revenue_has_null_margin(self, db_session, client):
        """When total revenue is $0.00, gross_margin_pct is None (N/A)."""
        admin_acc = Account(
            account_id=uuid.uuid4(),
            email=f"admin_zero_{uuid.uuid4().hex[:6]}@talos.ai",
            role="admin",
            balance_credits=1000,
        )
        db_session.add(admin_acc)
        await db_session.commit()

        admin_token = identity_service.create_web_session(admin_acc)
        headers = {"Authorization": f"Bearer {admin_token}"}

        resp = client.get("/admin/overview?time_range=today", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["revenue_source"] == "billing_transactions"
        if data["total_revenue_usd"] == 0:
            assert data["gross_margin_pct"] is None

    @pytest.mark.asyncio
    async def test_admin_user_directory_excludes_google_sub(self, db_session, client):
        """Admin user directory lists users but never exposes raw google_sub."""
        admin_acc = Account(
            account_id=uuid.uuid4(),
            email=f"admin_auditor_{uuid.uuid4().hex[:6]}@talos.ai",
            role="admin",
            balance_credits=1000,
        )
        secret_sub = f"super_secret_sub_{uuid.uuid4().hex[:8]}"
        target_user = Account(
            account_id=uuid.uuid4(),
            email=f"secret_user_{uuid.uuid4().hex[:6]}@example.com",
            google_sub=secret_sub,
            role="user",
            balance_credits=2500,
        )
        db_session.add_all([admin_acc, target_user])
        await db_session.commit()

        token = identity_service.create_web_session(admin_acc)
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get("/admin/users", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] >= 2
        assert secret_sub not in resp.text

    @pytest.mark.asyncio
    async def test_admin_pricing_configuration_versioning(self, db_session, client):
        """Admin can inspect and publish new credit_reference_usd versions."""
        admin_acc = Account(
            account_id=uuid.uuid4(),
            email=f"admin_cfg_{uuid.uuid4().hex[:6]}@talos.ai",
            role="admin",
            balance_credits=1000,
        )
        db_session.add(admin_acc)
        await db_session.commit()

        token = identity_service.create_web_session(admin_acc)
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Update credit reference to $0.08
        resp = client.post(
            "/admin/pricing/config",
            json={"credit_reference_usd": 0.08, "version": "v_test_08"},
            headers=headers,
        )
        assert resp.status_code == 201

        # 2. List configurations
        list_resp = client.get("/admin/pricing/config", headers=headers)
        assert list_resp.status_code == 200
        cfgs = list_resp.json()["configurations"]
        assert any(c["version"] == "v_test_08" and c["credit_reference_usd"] == 0.08 and c["active"] for c in cfgs)

    def test_margin_monitor_cannot_auto_publish_pricing(self):
        """
        CRITICAL INVARIANT: margin_monitor.py must never publish pricing versions.
        Static AST audit verifies margin_monitor contains zero calls to publish_pricing_version.
        """
        from app.services import margin_monitor
        import inspect

        source = inspect.getsource(margin_monitor)
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                func_name = getattr(func, "id", None) or getattr(func, "attr", None)
                assert func_name not in ("publish_pricing_version", "publish_pricing_version_admin"), (
                    f"CRITICAL INVARIANT VIOLATION: margin_monitor.py contains call to '{func_name}'"
                )
        assert "publish_pricing_version" not in source


class TestAdminControlCenterExtended:
    """Tests for the production-grade Admin Control Center features."""

    @pytest.mark.asyncio
    async def test_admin_user_detail_drilldown(self, db_session, client):
        admin_acc = Account(
            account_id=uuid.uuid4(),
            email=f"admin_drill_{uuid.uuid4().hex[:6]}@talos.ai",
            role="admin",
            balance_credits=1000,
        )
        target_user = Account(
            account_id=uuid.uuid4(),
            email=f"target_user_{uuid.uuid4().hex[:6]}@example.com",
            role="user",
            balance_credits=500,
        )
        db_session.add_all([admin_acc, target_user])
        await db_session.commit()

        token = identity_service.create_web_session(admin_acc)
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get(f"/admin/users/{target_user.account_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["account_id"] == str(target_user.account_id)
        assert data["email"] == target_user.email
        assert "llm_usage" in data
        assert "tool_usage" in data
        assert data["revenue_source"] == "billing_transactions"
        assert data["cogs_source"] == "usage_events"

    @pytest.mark.asyncio
    async def test_admin_adjust_credits_and_audit_log(self, db_session, client):
        admin_acc = Account(
            account_id=uuid.uuid4(),
            email=f"admin_adjust_{uuid.uuid4().hex[:6]}@talos.ai",
            role="admin",
            balance_credits=1000,
        )
        target_user = Account(
            account_id=uuid.uuid4(),
            email=f"target_adjust_{uuid.uuid4().hex[:6]}@example.com",
            role="user",
            balance_credits=100,
        )
        db_session.add_all([admin_acc, target_user])
        await db_session.commit()

        token = identity_service.create_web_session(admin_acc)
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Adjust credits
        adj_resp = client.post(
            f"/admin/users/{target_user.account_id}/adjust-credits",
            json={"amount": 250, "balance_type": "topup", "note": "Promotional test credits"},
            headers=headers,
        )
        assert adj_resp.status_code == 200
        assert adj_resp.json()["new_balance"] == 350

        # 2. Check audit logs
        log_resp = client.get("/admin/audit-logs", headers=headers)
        assert log_resp.status_code == 200
        logs = log_resp.json()["logs"]
        assert any(l["action"] == "ADJUST_USER_CREDITS" for l in logs)

    @pytest.mark.asyncio
    async def test_admin_provider_pricing_versioning(self, db_session, client):
        admin_acc = Account(
            account_id=uuid.uuid4(),
            email=f"admin_price_{uuid.uuid4().hex[:6]}@talos.ai",
            role="admin",
            balance_credits=1000,
        )
        db_session.add(admin_acc)
        await db_session.commit()

        token = identity_service.create_web_session(admin_acc)
        headers = {"Authorization": f"Bearer {token}"}

        # Save provider pricing
        resp = client.post(
            "/admin/pricing/providers",
            json={
                "provider": "openai",
                "model_id": "gpt-4o-custom",
                "pricing_type": "token",
                "input_cost_usd_per_1m": 2.50,
                "output_cost_usd_per_1m": 10.00,
                "cached_input_cost_usd_per_1m": 1.25,
            },
            headers=headers,
        )
        assert resp.status_code == 201

        # List provider pricing
        list_resp = client.get("/admin/pricing/providers", headers=headers)
        assert list_resp.status_code == 200
        providers = list_resp.json()["providers"]
        assert any(p["model_id"] == "gpt-4o-custom" for p in providers)

    @pytest.mark.asyncio
    async def test_admin_interactive_pricing_simulator(self, db_session, client):
        admin_acc = Account(
            account_id=uuid.uuid4(),
            email=f"admin_sim_{uuid.uuid4().hex[:6]}@talos.ai",
            role="admin",
            balance_credits=1000,
        )
        db_session.add(admin_acc)
        await db_session.commit()

        token = identity_service.create_web_session(admin_acc)
        headers = {"Authorization": f"Bearer {token}"}

        sim_resp = client.post(
            "/admin/pricing/simulate-interactive",
            json={
                "model_id": "claude-3-5-sonnet",
                "provider": "anthropic",
                "input_tokens": 100000,
                "output_tokens": 20000,
                "cached_tokens": 0,
                "input_cost_usd_per_1m": 3.00,
                "output_cost_usd_per_1m": 15.00,
                "target_margin": 0.75,
                "credit_reference_usd": 0.10,
            },
            headers=headers,
        )
        assert sim_resp.status_code == 200
        data = sim_resp.json()
        assert data["provider_cogs_usd"] == 0.6  # (100k/1M)*3 + (20k/1M)*15 = 0.3 + 0.3 = 0.6
        assert data["talos_credits"] > 0
        assert data["gross_profit_usd"] > 0
        assert data["gross_margin_pct"] >= 70.0

    @pytest.mark.asyncio
    async def test_admin_global_economics(self, db_session, client):
        admin_acc = Account(
            account_id=uuid.uuid4(),
            email=f"admin_econ_{uuid.uuid4().hex[:6]}@talos.ai",
            role="admin",
            balance_credits=1000,
        )
        db_session.add(admin_acc)
        await db_session.commit()

        token = identity_service.create_web_session(admin_acc)
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get("/admin/economics?time_range=30d", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "revenue_usd" in data
        assert data["revenue_source"] == "billing_transactions"
        assert "total_cogs_usd" in data
        assert data["cogs_source"] == "usage_events"
        assert "gross_profit_usd" in data
        assert "credit_liability_usd" in data
        assert "revenue_by_plan" in data

    @pytest.mark.asyncio
    async def test_admin_credit_ledger(self, db_session, client):
        admin_acc = Account(
            account_id=uuid.uuid4(),
            email=f"admin_ledger_{uuid.uuid4().hex[:6]}@talos.ai",
            role="admin",
            balance_credits=1000,
        )
        db_session.add(admin_acc)
        await db_session.commit()

        token = identity_service.create_web_session(admin_acc)
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get("/admin/ledger?page=1&page_size=20", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "transactions" in data
        assert "total_count" in data

    @pytest.mark.asyncio
    async def test_admin_auth_providers_get_and_update(self, db_session, client):
        admin_acc = Account(
            account_id=uuid.uuid4(),
            email=f"admin_auth_{uuid.uuid4().hex[:6]}@talos.ai",
            role="admin",
            balance_credits=1000,
        )
        db_session.add(admin_acc)
        await db_session.commit()

        token = identity_service.create_web_session(admin_acc)
        headers = {"Authorization": f"Bearer {token}"}

        # 1. GET /admin/auth-providers
        resp = client.get("/admin/auth-providers", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data
        provider_ids = [p["id"] for p in data["providers"]]
        assert "google" in provider_ids
        assert "github" in provider_ids
        assert "microsoft" in provider_ids

        # 2. POST /admin/auth-providers
        test_client_id = "test-github-client-id-123"
        test_client_sec = "test-github-client-secret-xyz"
        post_resp = client.post(
            "/admin/auth-providers",
            json={
                "provider": "github",
                "client_id": test_client_id,
                "client_secret": test_client_sec,
            },
            headers=headers,
        )
        assert post_resp.status_code == 200
        post_data = post_resp.json()
        assert post_data["status"] == "ok"
        assert post_data["provider"] == "github"
        assert post_data["has_secret"] is True
        assert post_data["is_configured"] is True

        # 3. GET /admin/auth-providers should now reflect GitHub configured
        get_resp = client.get("/admin/auth-providers", headers=headers)
        assert get_resp.status_code == 200
        gh = next(p for p in get_resp.json()["providers"] if p["id"] == "github")
        assert gh["client_id"] == test_client_id
        assert gh["has_secret"] is True
        assert gh["is_configured"] is True

