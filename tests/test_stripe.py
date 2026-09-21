"""
Tests for Phase 8: Stripe Billing Integration & Webhooks.

Tests:
  - Top-up credit checkout webhook grants credits
  - Subscription checkout webhook grants credits and sets tier
  - Webhook deduplication: duplicate delivery ignored
"""

import uuid
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select


@pytest_asyncio.fixture(scope="module")
async def client_and_account():
    from app.main import app
    from app.routers.relay import get_authenticated_account
    from app.models.accounts import Account
    from app.database import get_session_factory

    test_account = Account(
        account_id=uuid.uuid4(),
        email=f"stripe_test_{uuid.uuid4().hex[:6]}@example.com",
        balance_credits=0,
        subscription_tier="free",
    )

    app.dependency_overrides[get_authenticated_account] = lambda: test_account
    with TestClient(app) as c:
        factory = get_session_factory()
        async with factory() as db:
            db.add(test_account)
            await db.commit()

        yield c, test_account
    app.dependency_overrides.clear()


class TestStripeBilling:
    def test_create_checkout_session(self, client_and_account):
        client, account = client_and_account
        resp = client.post(
            "/billing/checkout-session",
            json={"package_type": "topup", "credits": 500, "amount_cents": 500},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "checkout_url" in data
        assert data["credits"] == 500

    def test_webhook_topup_completed(self, client_and_account):
        client, account = client_and_account
        event_id = f"evt_{uuid.uuid4().hex}"

        webhook_payload = {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "mode": "payment",
                    "client_reference_id": str(account.account_id),
                    "metadata": {"credits": "500"},
                }
            },
        }

        resp = client.post("/billing/webhook", json=webhook_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "topup_granted"
        assert data["credits"] == 500

        # Verify balance updated via dashboard/balance
        bal_resp = client.get("/dashboard/balance")
        assert bal_resp.json()["total"] == 500

    def test_webhook_idempotency_duplicate_ignored(self, client_and_account):
        """Duplicate webhook event with same event_id must not double-credit."""
        client, account = client_and_account
        event_id = f"evt_dup_{uuid.uuid4().hex}"

        webhook_payload = {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "mode": "payment",
                    "client_reference_id": str(account.account_id),
                    "metadata": {"credits": "300"},
                }
            },
        }

        # First delivery
        resp1 = client.post("/billing/webhook", json=webhook_payload)
        assert resp1.status_code == 200
        assert resp1.json()["action"] == "topup_granted"

        # Duplicate delivery
        resp2 = client.post("/billing/webhook", json=webhook_payload)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "duplicate_ignored"

    def test_webhook_subscription_started(self, client_and_account):
        client, account = client_and_account
        event_id = f"evt_sub_{uuid.uuid4().hex}"

        webhook_payload = {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "mode": "subscription",
                    "client_reference_id": str(account.account_id),
                    "metadata": {"credits": "2000", "tier": "pro"},
                }
            },
        }

        resp = client.post("/billing/webhook", json=webhook_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "subscription_granted"
        assert data["credits"] == 2000
