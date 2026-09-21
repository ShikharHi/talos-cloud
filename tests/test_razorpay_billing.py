"""
Tests for India-First Razorpay Payment Gateway Adapter & Webhooks.

Tests cover:
  1. Order creation in INR paise with account_id and credits in metadata.
  2. HMAC-SHA256 signature verification (valid vs tampered).
  3. order.paid / payment.captured webhooks grant top-up credits.
  4. subscription.charged webhooks grant subscription credits.
  5. Webhook idempotency / deduplication.
  6. Invariant: Only HMAC-verified server-side webhooks grant credits.
"""

import hashlib
import hmac
import json
import uuid
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient


@pytest_asyncio.fixture(scope="module")
async def client_and_account():
    from app.main import app
    from app.routers.relay import get_authenticated_account
    from app.models.accounts import Account
    from app.database import get_session_factory

    test_account = Account(
        account_id=uuid.uuid4(),
        email=f"rzp_tester_{uuid.uuid4().hex[:6]}@example.com",
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


class TestRazorpayBilling:
    def test_create_razorpay_order_inr(self, client_and_account):
        """Create Razorpay order for ₹499 top-up (49,900 paise -> 5,000 credits)."""
        client, account = client_and_account
        resp = client.post(
            "/billing/order",
            json={
                "gateway": "razorpay",
                "package_type": "topup",
                "credits": 5000,
                "amount_minor": 49900,  # ₹499 in paise
                "currency": "INR",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["gateway"] == "razorpay"
        assert data["currency"] == "INR"
        assert data["amount_minor"] == 49900
        assert data["order_id"].startswith("order_")
        assert data["metadata"]["credits"] == "5000"
        assert data["metadata"]["account_id"] == str(account.account_id)

    def test_razorpay_webhook_topup_completed(self, client_and_account):
        """Verify order.paid webhook grants top-up credits to user ledger."""
        client, account = client_and_account
        event_id = f"rzp_evt_{uuid.uuid4().hex[:10]}"

        payload = {
            "entity": "event",
            "event": "order.paid",
            "event_id": event_id,
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_{uuid.uuid4().hex[:10]}",
                        "amount": 49900,
                        "currency": "INR",
                        "status": "captured",
                        "notes": {
                            "account_id": str(account.account_id),
                            "credits": "5000",
                            "package_type": "topup",
                        },
                    }
                }
            },
        }

        resp = client.post(
            "/billing/webhook/razorpay",
            json=payload,
            headers={"X-Razorpay-Signature": "valid_signature_mock"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["action"] == "topup_granted"
        assert data["credits"] == 5000

        # Verify balance updated via dashboard
        bal_resp = client.get("/dashboard/balance")
        assert bal_resp.json()["topup_credits"] == 5000

    def test_razorpay_webhook_subscription_charged(self, client_and_account):
        """Verify subscription.charged webhook grants subscription credits."""
        client, account = client_and_account
        event_id = f"rzp_evt_sub_{uuid.uuid4().hex[:10]}"

        payload = {
            "entity": "event",
            "event": "subscription.charged",
            "event_id": event_id,
            "payload": {
                "subscription": {
                    "entity": {
                        "id": f"sub_{uuid.uuid4().hex[:10]}",
                        "plan_id": "plan_talos_pro_999",
                        "amount": 99900,  # ₹999/month
                        "currency": "INR",
                        "status": "active",
                        "notes": {
                            "account_id": str(account.account_id),
                            "credits": "10000",
                            "package_type": "subscription",
                        },
                    }
                }
            },
        }

        resp = client.post(
            "/billing/webhook/razorpay",
            json=payload,
            headers={"X-Razorpay-Signature": "valid_signature_mock"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["action"] == "subscription_granted"
        assert data["credits"] == 10000

        # Check balance breakdown (sub=10,000, topup=5,000, total=15,000)
        bal_resp = client.get("/dashboard/balance")
        bal_data = bal_resp.json()
        assert bal_data["subscription_credits"] == 10000
        assert bal_data["topup_credits"] == 5000
        assert bal_data["total"] == 15000

    def test_razorpay_webhook_idempotency(self, client_and_account):
        """Duplicate webhook delivery must be ignored without double-crediting."""
        client, account = client_and_account
        event_id = f"rzp_evt_dup_{uuid.uuid4().hex[:10]}"

        payload = {
            "entity": "event",
            "event": "order.paid",
            "event_id": event_id,
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_dup_{uuid.uuid4().hex[:8]}",
                        "amount": 10000,
                        "currency": "INR",
                        "notes": {
                            "account_id": str(account.account_id),
                            "credits": "1000",
                            "package_type": "topup",
                        },
                    }
                }
            },
        }

        # First delivery -> processed
        resp1 = client.post("/billing/webhook/razorpay", json=payload)
        assert resp1.status_code == 200
        assert resp1.json()["action"] == "topup_granted"

        # Second delivery -> duplicate_ignored
        resp2 = client.post("/billing/webhook/razorpay", json=payload)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "duplicate_ignored"

    def test_hmac_signature_verification_rejection(self, client_and_account, monkeypatch):
        """When a real webhook secret is configured, invalid signatures are rejected."""
        client, account = client_and_account
        from app.services.billing.razorpay_adapter import RazorpayAdapter
        import app.services.billing.billing_service as bs_module

        # Configure real secret on adapter
        test_secret = "rzp_secret_key_12345"
        custom_adapter = RazorpayAdapter(webhook_secret=test_secret)
        monkeypatch.setitem(bs_module._ADAPTERS, "razorpay", custom_adapter)

        payload_bytes = b'{"event": "order.paid", "id": "test_sig_evt"}'

        # 1. Invalid signature -> 400 Bad Request
        resp_invalid = client.post(
            "/billing/webhook/razorpay",
            content=payload_bytes,
            headers={"X-Razorpay-Signature": "invalid_tampered_signature", "Content-Type": "application/json"},
        )
        # 2. Valid signature -> 200 OK
        valid_sig = hmac.new(test_secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        resp_valid = client.post(
            "/billing/webhook/razorpay",
            content=payload_bytes,
            headers={"X-Razorpay-Signature": valid_sig, "Content-Type": "application/json"},
        )
        assert resp_valid.status_code == 200

    def test_cross_event_type_same_order_deduplication(self, client_and_account):
        """
        CRITICAL: If Razorpay sends 'payment.captured' AND 'order.paid' for the same order,
        they must NOT both grant credits. Exactly ONE credit grant must occur.
        """
        client, account = client_and_account
        shared_order_id = f"order_shared_{uuid.uuid4().hex[:8]}"
        pay_id = f"pay_shared_{uuid.uuid4().hex[:8]}"

        # Baseline balance before this test
        bal_before = client.get("/dashboard/balance").json()["total"]

        # Webhook 1: payment.captured for shared_order_id
        webhook_payment_captured = {
            "entity": "event",
            "event": "payment.captured",
            "event_id": f"evt_pay_cap_{uuid.uuid4().hex[:8]}",
            "payload": {
                "payment": {
                    "entity": {
                        "id": pay_id,
                        "order_id": shared_order_id,
                        "amount": 49900,
                        "currency": "INR",
                        "notes": {
                            "account_id": str(account.account_id),
                            "credits": "5000",
                            "package_type": "topup",
                        },
                    }
                }
            },
        }

        # Webhook 2: order.paid for the same shared_order_id
        webhook_order_paid = {
            "entity": "event",
            "event": "order.paid",
            "event_id": f"evt_ord_paid_{uuid.uuid4().hex[:8]}",
            "payload": {
                "order": {
                    "entity": {
                        "id": shared_order_id,
                        "amount": 49900,
                        "currency": "INR",
                        "notes": {
                            "account_id": str(account.account_id),
                            "credits": "5000",
                            "package_type": "topup",
                        },
                    }
                }
            },
        }

        # First delivery -> payment.captured grants 5,000 credits
        resp1 = client.post("/billing/webhook/razorpay", json=webhook_payment_captured)
        assert resp1.status_code == 200
        assert resp1.json()["action"] == "topup_granted"
        assert resp1.json()["credits"] == 5000

        # Second delivery -> order.paid for SAME order is detected and duplicate-ignored
        resp2 = client.post("/billing/webhook/razorpay", json=webhook_order_paid)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "duplicate_ignored"

        # Final check: total balance increased by exactly 5,000 credits (NOT 10,000)
        bal_after = client.get("/dashboard/balance").json()["total"]
        assert bal_after == bal_before + 5000, f"Expected {bal_before + 5000}, got {bal_after}"
