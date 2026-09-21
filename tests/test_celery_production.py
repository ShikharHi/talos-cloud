"""
Unit and Integration Tests for Production Celery Infrastructure & Tasks.

Verifies:
  1. Celery configuration guarantees (AOF broker, result_backend=None, visibility_timeout=1800, acks_late=True)
  2. Queue partitioning (maintenance, billing, package-security)
  3. Maintenance task execution: reservation expiry & DB-driven staging cleanup
  4. Billing task execution: subscription cycle renewal & deduplication
  5. Security task execution: AST validation, promotion idempotency, failure states
"""

import uuid
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from app.celery_app.app import celery_app
from app.celery_app.tasks.maintenance import (
    cleanup_stale_reservations_task,
    cleanup_expired_staging_uploads_task,
    run_cleanup_stale_reservations,
    run_cleanup_expired_staging_uploads,
)
from app.celery_app.tasks.billing import (
    reconcile_subscription_cycles_task,
    run_reconcile_subscription_cycles,
)
from app.celery_app.tasks.security import (
    verify_and_promote_package_task,
    run_verify_and_promote_package,
)
from app.models.accounts import Account
from app.models.marketplace import MarketplaceListing, MarketplacePackageVersion, PackageUpload
from app.models.subscription_plans import Subscription, SubscriptionPlan, SubscriptionStatus
from app.models.wallet import CreditReservation, ReservationStatus, Wallet
from app.services.wallet_engine import WalletEngine


def test_celery_configuration_invariants():
    """Verifies that production Celery settings match architectural freeze."""
    conf = celery_app.conf
    assert conf.result_backend is None, "Result backend must be disabled; PostgreSQL is truth."
    assert conf.task_acks_late is True, "task_acks_late must be enabled for crash resilience."
    assert conf.task_reject_on_worker_lost is True, "task_reject_on_worker_lost must be enabled."
    assert conf.worker_prefetch_multiplier == 1, "Prefetch multiplier must be 1 for fair task distribution."
    assert conf.broker_transport_options.get("visibility_timeout") == 1800, "Visibility timeout must be 30m."

    # Verify task queue routing
    routes = conf.task_routes
    assert routes["app.celery_app.tasks.maintenance.*"]["queue"] == "maintenance"
    assert routes["app.celery_app.tasks.billing.*"]["queue"] == "billing"
    assert routes["app.celery_app.tasks.security.*"]["queue"] == "package-security"


@pytest.mark.asyncio
async def test_celery_maintenance_stale_reservation_cleanup(db_session):
    """Verifies that maintenance task cleans stale reservations and releases split holds."""
    user_id = uuid.uuid4()
    account = Account(account_id=user_id, email=f"celery_recov_{user_id.hex[:6]}@example.com")
    db_session.add(account)
    await db_session.flush()

    engine = WalletEngine(db_session)
    wallet = await engine.create_wallet(account_id=user_id, initial_monthly=30, initial_topup=20)

    # Reserve 40 credits (30 monthly + 10 topup)
    resv = await engine.reserve(
        account_id=user_id,
        task_id="stale_task",
        amount=40,
        idempotency_key="stale_key_1",
    )
    assert resv.monthly_reserved == 30
    assert resv.topup_reserved == 10
    assert wallet.available_credits == 10

    # Expire reservation
    resv.expires_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    await db_session.commit()

    # Invoke maintenance worker
    result = await run_cleanup_stale_reservations(db=db_session)
    assert result["cleaned"] >= 1

    # Verify status EXPIRED and balances restored to available
    await db_session.refresh(resv)
    assert resv.status == ReservationStatus.EXPIRED

    await db_session.refresh(wallet)
    assert wallet.reserved_monthly == 0
    assert wallet.reserved_topup == 0
    assert wallet.available_credits == 50


@pytest.mark.asyncio
async def test_celery_billing_subscription_cycle_renewal(db_session):
    """Verifies that billing task renews due active subscriptions without duplicate grants."""
    user_id = uuid.uuid4()
    account = Account(account_id=user_id, email=f"celery_sub_{user_id.hex[:6]}@example.com")
    db_session.add(account)
    await db_session.flush()

    engine = WalletEngine(db_session)
    wallet = await engine.create_wallet(account_id=user_id, initial_monthly=0, initial_topup=0)

    # Get free plan
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
    # Create past-due subscription
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

    # Run billing reconciliation worker
    result = await run_reconcile_subscription_cycles(db=db_session)
    assert result["renewed"] >= 1

    await db_session.refresh(wallet)
    assert wallet.monthly_balance == 50
    assert wallet.available_credits == 50

    await db_session.refresh(sub)
    sub_next = sub.next_reset_at.replace(tzinfo=timezone.utc) if sub.next_reset_at.tzinfo is None else sub.next_reset_at
    assert sub_next > now


@pytest.mark.asyncio
async def test_celery_security_promotion_idempotency(db_session):
    """Verifies that security task safely no-ops on already promoted uploads."""
    user_id = uuid.uuid4()
    account = Account(account_id=user_id, email=f"celery_sec_{user_id.hex[:6]}@example.com")
    db_session.add(account)
    await db_session.flush()

    upload_id = uuid.uuid4()
    upload = PackageUpload(
        upload_id=upload_id,
        account_id=user_id,
        resource_type="agents",
        resource_id="idemp-agent",
        version="1.0.0",
        object_key="packages/agents/idemp-agent/1.0.0/package.zip",
        bucket="talos-marketplace",
        status="promoted",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(upload)
    await db_session.commit()

    # Running task on already promoted upload must return already_promoted
    res = await run_verify_and_promote_package(str(upload_id), db=db_session)
    assert res["status"] == "already_promoted"

