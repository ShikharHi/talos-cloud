"""
Tests for Phase 4: Pricing Engine admin endpoints.

Critical test: margin_monitor must not be able to call the admin publish endpoint
even if it imports the router module.
"""

import pytest
import pytest_asyncio
import uuid
from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """TestClient for the full app with admin secret set."""
    import os
    os.environ["TALOS_ADMIN_SECRET"] = "test-admin-secret"
    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import app
    from app.routers.relay import get_authenticated_account
    from app.models.accounts import Account

    # Override auth so endpoints that need it work
    mock_account = Account(
        account_id=uuid.uuid4(),
        email="admin@example.com",
        balance_credits=10000,
        subscription_tier="pro",
    )
    app.dependency_overrides[get_authenticated_account] = lambda: mock_account

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    get_settings.cache_clear()


ADMIN_HEADERS = {"x-admin-secret": "test-admin-secret"}

VALID_SCHEDULE_YAML = """
version: v_test
capabilities:
  reasoning_model:
    unit: per_1k_tokens
    credits: 12
    target_margin: 0.40
  web_search:
    unit: per_call
    credits: 4
    target_margin: 0.50
"""


class TestPricingAdmin:

    def test_publish_new_version(self, client):
        """Publishing a new version creates it and marks it active."""
        resp = client.post(
            "/admin/pricing/publish",
            json={
                "version": f"v_test_{uuid.uuid4().hex[:6]}",
                "schedule_yaml": VALID_SCHEDULE_YAML,
                "published_by": "admin@example.com",
                "effective_from": "2026-09-01T00:00:00Z",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["is_active"] is True
        assert data["published_by"] == "admin@example.com"
        # version_id present
        assert "version_id" in data

    def test_publish_requires_admin_secret(self, client):
        """Publishing without admin secret is rejected."""
        resp = client.post(
            "/admin/pricing/publish",
            json={
                "version": "v_unauth",
                "schedule_yaml": VALID_SCHEDULE_YAML,
                "published_by": "nobody",
                "effective_from": "2026-09-01T00:00:00Z",
            },
            headers={"x-admin-secret": "wrong-secret"},
        )
        assert resp.status_code == 403

    def test_publish_invalid_yaml_rejected(self, client):
        """Malformed YAML must be rejected with 400."""
        resp = client.post(
            "/admin/pricing/publish",
            json={
                "version": "v_bad_yaml",
                "schedule_yaml": "not: valid: yaml: ::::",
                "published_by": "admin",
                "effective_from": "2026-09-01T00:00:00Z",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400

    def test_list_versions(self, client):
        """GET /admin/pricing/versions returns a list."""
        resp = client.get("/admin/pricing/versions", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_active_version(self, client):
        """GET /admin/pricing/active returns the active version."""
        resp = client.get("/admin/pricing/active", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_active"] is True

    def test_compute_suggested_price(self, client):
        """Price computation endpoint returns a schedule_price_example."""
        resp = client.get(
            "/admin/pricing/compute-price/reasoning_model",
            params={"real_cost_per_unit": 0.001, "target_margin": 0.40},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "schedule_price_example" in data
        # price > cost (margin applied)
        assert data["schedule_price_example"] > 0.001

    def test_margin_monitor_cannot_call_publish(self, client):
        """
        INVARIANT: margin_monitor module must not import or call anything
        from the pricing admin router. Verify by checking the module's source
        has no reference to 'publish_pricing_version' or 'admin/pricing'.
        """
        import inspect
        from app.services import margin_monitor
        source = inspect.getsource(margin_monitor)
        forbidden = [
            "publish_pricing_version",
            "admin/pricing",
            "PublishPricingVersion",
            "PricingVersion(",  # creating a new PricingVersion row
        ]
        for pattern in forbidden:
            assert pattern not in source, (
                f"margin_monitor.py references '{pattern}' — "
                "the monitor must NEVER call pricing admin functions."
            )
