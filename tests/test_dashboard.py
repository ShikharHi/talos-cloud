"""
Tests for Phase 7: Dashboard API endpoints.

CRITICAL INVARIANTS:
1. The 'provider' field must NEVER appear in any /dashboard/* response body.
2. Capability usage is labeled using abstract names (e.g. 'Talos Reasoning').
"""

import json
import uuid
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

FORBIDDEN_PROVIDER_STRINGS = ["anthropic", "openai", "tavily", "groq", "zhipu", "z.ai"]


@pytest_asyncio.fixture(scope="module")
async def client_with_mock_user():
    from app.main import app
    from app.routers.relay import get_authenticated_account
    from app.models.accounts import Account
    from app.database import get_session_factory

    test_account = Account(
        account_id=uuid.uuid4(),
        email=f"dashboard_tester_{uuid.uuid4().hex[:8]}@example.com",
        balance_credits=250,
        subscription_tier="pro",
    )

    app.dependency_overrides[get_authenticated_account] = lambda: test_account
    with TestClient(app) as c:
        factory = get_session_factory()
        async with factory() as db:
            db.add(test_account)
            await db.commit()

        yield c, test_account
    app.dependency_overrides.clear()


class TestDashboard:
    def test_dashboard_balance(self, client_with_mock_user):
        client, account = client_with_mock_user
        resp = client.get("/dashboard/balance")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "subscription_credits" in data
        assert "topup_credits" in data
        assert "total" in data

    def test_dashboard_usage(self, client_with_mock_user):
        client, account = client_with_mock_user
        resp = client.get("/dashboard/usage")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "items" in data
        assert "total_credits_charged" in data

    def test_dashboard_task_cost(self, client_with_mock_user):
        client, account = client_with_mock_user
        task_id = "test-task-123"
        resp = client.get(f"/dashboard/tasks/{task_id}/cost")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["task_id"] == task_id
        assert "total_credits_spent" in data
        assert "events" in data

    def test_provider_field_never_in_dashboard_response(self, client_with_mock_user):
        """
        INVARIANT: Scan raw HTTP response bytes across all dashboard endpoints
        to ensure 'provider' field and provider strings never leak to the client.
        """
        client, account = client_with_mock_user

        endpoints = [
            "/dashboard/balance",
            "/dashboard/usage",
            "/dashboard/tasks/sample-task-001/cost",
        ]

        for ep in endpoints:
            resp = client.get(ep)
            raw_text = resp.text.lower()
            assert '"provider"' not in raw_text, f"Leaked '\"provider\"' key in {ep}: {raw_text}"
            for forbidden in FORBIDDEN_PROVIDER_STRINGS:
                assert forbidden not in raw_text, f"Leaked provider name '{forbidden}' in {ep}: {raw_text}"
