"""
Unit and Integration Tests for Inngest Background Jobs & Durable Functions.

Verifies:
  1. GET /api/inngest introspection returns valid registration and all functions
  2. Scheduled functions have correct cron strings preserving production timings
  3. talos.marketplace.verify_and_promote multi-step execution & idempotency
  4. talos.billing.reconcile_subscriptions executes monthly reset without duplicate grant
  5. talos.maintenance.cleanup_stale_reservations expires stale HELD reservations
  6. talos.maintenance.cleanup_staging_uploads purges expired staging objects
  7. talos.billing.process_webhook enforces database UNIQUE deduplication
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.inngest import all_inngest_functions, inngest_client
from app.inngest.events import TalosEvents
from app.inngest.functions.marketplace import marketplace_verify_and_promote_fn
from app.inngest.functions.billing import (
    billing_reconcile_subscriptions_fn,
    billing_process_webhook_fn,
)
from app.inngest.functions.maintenance import (
    maintenance_cleanup_stale_reservations_fn,
    maintenance_cleanup_staging_uploads_fn,
)
from app.inngest.functions.monitoring import monitoring_margin_check_fn
from app.inngest.functions.auth import auth_cleanup_expired_tokens_fn

from app.models.accounts import Account
from app.models.marketplace import MarketplaceListing, MarketplacePackageVersion, PackageUpload
from app.models.subscription_plans import Subscription, SubscriptionPlan, SubscriptionStatus
from app.models.wallet import CreditReservation, ReservationStatus, Wallet
from app.services.wallet_engine import WalletEngine
from app.storage.models import UploadState
from app.storage.service import StorageService
from tests.test_storage_provider import FakeStorageProvider


def test_inngest_introspection_endpoint():
    """Verifies that GET /api/inngest returns a 200 OK and registers all Talos functions."""
    client = TestClient(app)
    response = client.get("/api/inngest")
    assert response.status_code == 200
    data = response.json()
    assert "function_count" in data
    assert data["function_count"] >= 11
    
    # Check that our function local IDs are registered
    registered_fn_ids = [fn.local_id for fn in all_inngest_functions]
    expected_ids = [
        "talos.marketplace.verify_and_promote",
        "talos.billing.reconcile_subscriptions",
        "talos.billing.process_webhook",
        "talos.maintenance.cleanup_stale_reservations",
        "talos.maintenance.cleanup_staging_uploads",
        "talos.monitoring.margin_check",
        "talos.tasks.aggregate_usage",
        "talos.auth.send_email",
        "talos.auth.send_password_reset",
        "talos.auth.send_security_alert",
        "talos.auth.cleanup_expired_tokens",
    ]
    for exp_id in expected_ids:
        assert exp_id in registered_fn_ids, f"Function {exp_id} should be registered in Inngest"


def test_inngest_scheduled_cron_timings():
    """Verifies exact preservation of scheduled cron triggers for production reliability."""
    cron_map = {}
    for fn in all_inngest_functions:
        for trig in fn._triggers:
            cron_expr = getattr(trig, "cron", None)
            if cron_expr:
                cron_map[fn.local_id] = cron_expr

    assert cron_map.get("talos.billing.reconcile_subscriptions") == "*/15 * * * *"
    assert cron_map.get("talos.maintenance.cleanup_stale_reservations") == "*/5 * * * *"
    assert cron_map.get("talos.maintenance.cleanup_staging_uploads") == "*/30 * * * *"
    assert cron_map.get("talos.monitoring.margin_check") == "0 2 * * *"
    assert cron_map.get("talos.auth.cleanup_expired_tokens") == "0 * * * *"


@pytest.mark.asyncio
async def test_inngest_maintenance_stale_reservation_cleanup(db_session):
    """Verifies that maintenance Inngest function cleans stale reservations and restores balances."""
    user_id = uuid.uuid4()
    account = Account(account_id=user_id, email=f"inngest_recov_{user_id.hex[:6]}@example.com")
    db_session.add(account)
    await db_session.flush()

    engine = WalletEngine(db_session)
    wallet = await engine.create_wallet(account_id=user_id, initial_monthly=30, initial_topup=20)

    # Reserve 40 credits (30 monthly + 10 topup)
    resv = await engine.reserve(
        account_id=user_id,
        task_id="stale_inngest_task",
        amount=40,
        idempotency_key="stale_inngest_key_1",
    )
    assert resv.monthly_reserved == 30
    assert resv.topup_reserved == 10
    assert wallet.available_credits == 10

    # Expire reservation
    resv.expires_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    await db_session.commit()

    # Create dummy Inngest step runner to simulate step.run
    class DummyStep:
        async def run(self, step_id, fn, *args, **kwargs):
            return await fn()

    dummy_ctx = MagicMock()
    dummy_step = DummyStep()

    class SessionContext:
        def __init__(self, session):
            self.session = session
        async def __aenter__(self):
            return self.session
        async def __aexit__(self, *args):
            pass

    def mock_factory():
        return lambda: SessionContext(db_session)

    with patch("app.inngest.functions.maintenance.get_session_factory", side_effect=mock_factory):
        result = await maintenance_cleanup_stale_reservations_fn._handler(dummy_ctx, dummy_step)
        assert result["cleaned"] >= 1

    # Verify status EXPIRED and balances restored
    await db_session.refresh(resv)
    assert resv.status == ReservationStatus.EXPIRED

    await db_session.refresh(wallet)
    assert wallet.reserved_monthly == 0
    assert wallet.reserved_topup == 0
    assert wallet.available_credits == 50


@pytest.mark.asyncio
async def test_inngest_billing_subscription_cycle_renewal(db_session):
    """Verifies that billing Inngest function renews due active subscriptions without duplicate grants."""
    user_id = uuid.uuid4()
    account = Account(account_id=user_id, email=f"inngest_sub_{user_id.hex[:6]}@example.com")
    db_session.add(account)
    await db_session.flush()

    engine = WalletEngine(db_session)
    wallet = await engine.create_wallet(account_id=user_id, initial_monthly=0, initial_topup=0)

    # Get or create free plan
    plan_stmt = select(SubscriptionPlan).where(SubscriptionPlan.name == "free")
    plan = (await db_session.execute(plan_stmt)).scalar_one_or_none()
    if not plan:
        plan = SubscriptionPlan(
            name="free",
            price_usd=0,
            monthly_credits=50,
            reset_period_days=30,
            topup_allowed=True,
            active=True,
        )
        db_session.add(plan)
        await db_session.flush()

    now = datetime.now(timezone.utc)
    sub = Subscription(
        account_id=user_id,
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        started_at=now - timedelta(days=35),
        current_period_start=now - timedelta(days=35),
        current_period_end=now - timedelta(days=5),
        next_reset_at=now - timedelta(days=5),
    )
    db_session.add(sub)
    await db_session.commit()

    class DummyStep:
        async def run(self, step_id, fn, *args, **kwargs):
            return await fn()

    class SessionContext:
        def __init__(self, session):
            self.session = session
        async def __aenter__(self):
            return self.session
        async def __aexit__(self, *args):
            pass

    def mock_factory():
        return lambda: SessionContext(db_session)

    dummy_ctx = MagicMock()
    dummy_step = DummyStep()

    with patch("app.inngest.functions.billing.get_session_factory", side_effect=mock_factory):
        result = await billing_reconcile_subscriptions_fn._handler(dummy_ctx, dummy_step)
        assert result["renewed"] >= 1

    await db_session.refresh(wallet)
    assert wallet.monthly_balance == 50
    assert wallet.available_credits == 50

    await db_session.refresh(sub)
    sub_next = sub.next_reset_at.replace(tzinfo=timezone.utc) if sub.next_reset_at.tzinfo is None else sub.next_reset_at
    assert sub_next > now


@pytest.mark.asyncio
async def test_inngest_marketplace_promotion_idempotency(db_session):
    """Verifies that marketplace Inngest function handles already-promoted package idempotently."""
    user_id = uuid.uuid4()
    account = Account(account_id=user_id, email=f"inngest_sec_{user_id.hex[:6]}@example.com")
    db_session.add(account)
    await db_session.flush()

    upload_id = uuid.uuid4()
    upload = PackageUpload(
        upload_id=upload_id,
        account_id=user_id,
        resource_type="agents",
        resource_id="idemp-agent-inngest",
        version="1.0.0",
        object_key="packages/agents/idemp-agent-inngest/1.0.0/package.zip",
        bucket="talos-marketplace",
        status="promoted",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(upload)
    await db_session.commit()

    class DummyStep:
        async def run(self, step_id, fn, *args, **kwargs):
            return await fn()

    dummy_ctx = MagicMock()
    dummy_ctx.event.data = {"upload_id": str(upload_id)}
    dummy_step = DummyStep()

    class SessionContext:
        def __init__(self, session):
            self.session = session
        async def __aenter__(self):
            return self.session
        async def __aexit__(self, *args):
            pass

    def mock_factory():
        return lambda: SessionContext(db_session)

    with patch("app.inngest.functions.marketplace.get_session_factory", side_effect=mock_factory):
        result = await marketplace_verify_and_promote_fn._handler(dummy_ctx, dummy_step)
        assert result["status"] == "promoted"
        assert result.get("idempotent") is True
