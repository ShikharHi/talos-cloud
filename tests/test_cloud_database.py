"""
Unit and Integration Tests for Production PostgreSQL Database Layer.

Tests:
  - Account creation with status, role, and email
  - UserInstall multi-installation, exact version persistence, and duplicate protection
  - Wallet available credits, reservations (HELD/COMMITTED/RELEASED), insufficient balance
  - Concurrent credit reservations safety
  - Append-only credit ledger auditability
  - StripeCustomer mapping and BillingTransaction recording
  - Database-backed Stripe webhook deduplication
  - Marketplace package version metadata (storage_key, sha256, permissions, requirements, compatibility)
  - UsageEvent metering and credit audit trail
"""

import asyncio
import uuid
from datetime import datetime, timezone
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.accounts import Account
from app.models.billing import BillingTransaction, StripeCustomer
from app.models.idempotency import WebhookEvent
from app.models.ledger import CreditTransaction, TransactionType
from app.models.marketplace import MarketplaceListing, MarketplacePackageVersion, UserInstall
from app.models.usage_event import UnitType, UsageEvent
from app.models.wallet import CreditReservation, ReservationStatus, Wallet
from app.services.stripe_service import process_stripe_webhook_event
from app.services.wallet_engine import InsufficientCreditsError, WalletEngine


@pytest.mark.asyncio
async def test_account_creation_and_status(db_session):
    """Verifies user account creation with default active status."""
    acc = Account(
        account_id=uuid.uuid4(),
        email="test_status_user@talos.dev",
        role="user",
        status="active",
        subscription_tier="free",
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)

    assert acc.status == "active"
    assert acc.role == "user"
    assert acc.email == "test_status_user@talos.dev"

    # Duplicate email must violate unique constraint
    dup_acc = Account(
        account_id=uuid.uuid4(),
        email="test_status_user@talos.dev",
        role="user",
    )
    db_session.add(dup_acc)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_user_install_exact_version_and_uniqueness(db_session):
    """
    Verifies user_installs stores exact installed version, tracks status,
    and prevents duplicate active records for the same (account_id, listing_id).
    """
    author = Account(email="author_install@talos.dev", role="user")
    user = Account(email="consumer_install@talos.dev", role="user")
    db_session.add_all([author, user])
    await db_session.commit()

    listing = MarketplaceListing(
        author_account_id=author.account_id,
        author_username="author_install",
        kind="agent",
        slug="finance-bot",
        display_name="Finance Bot",
        version="1.2.0",
        status="approved",
    )
    db_session.add(listing)
    await db_session.commit()

    # 1. Install exact version 1.2.0
    install = UserInstall(
        account_id=user.account_id,
        listing_id=listing.listing_id,
        installed_version="1.2.0",
        status="active",
    )
    db_session.add(install)
    await db_session.commit()
    await db_session.refresh(install)

    assert install.installed_version == "1.2.0"
    assert install.status == "active"
    assert install.installed_at is not None

    # 2. Duplicate install for same user and listing must fail unique constraint
    dup_install = UserInstall(
        account_id=user.account_id,
        listing_id=listing.listing_id,
        installed_version="1.3.0",
        status="active",
    )
    db_session.add(dup_install)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_wallet_available_credits_precision_and_lifecycle(db_session):
    """
    Tests Wallet available_credits, reservation, commit, release, and insufficient credits.
    """
    account = Account(email="wallet_lifecycle@talos.dev", role="user")
    db_session.add(account)
    await db_session.commit()
    account_id = account.account_id

    engine = WalletEngine(db_session)
    wallet = await engine.create_wallet(
        account_id=account_id,
        initial_monthly=50,
        initial_topup=25,
    )
    await db_session.commit()

    # available_credits preserves monthly + topup without coercion
    assert wallet.available_credits == 75

    # 1. Reserve 30 credits (should deduct from monthly first)
    res1 = await engine.reserve(
        account_id=account_id,
        task_id="task-101",
        amount=30,
        idempotency_key="idemp-res-1",
    )
    assert res1.status == ReservationStatus.HELD
    assert res1.amount_reserved == 30

    await db_session.refresh(wallet)
    assert wallet.available_monthly == 20
    assert wallet.reserved_monthly == 30
    assert wallet.topup_balance == 25
    assert wallet.available_credits == 45

    # Idempotent re-reservation returns same reservation without extra deduction
    res1_dup = await engine.reserve(
        account_id=account_id,
        task_id="task-101",
        amount=30,
        idempotency_key="idemp-res-1",
    )
    assert res1_dup.reservation_id == res1.reservation_id
    assert wallet.available_credits == 45

    # 2. Commit actual usage of 22 credits (refunds 8 credits)
    await engine.commit(res1.reservation_id, actual_amount=22)
    await db_session.commit()
    await db_session.refresh(wallet)

    assert res1.status == ReservationStatus.COMMITTED
    assert res1.amount_committed == 22
    assert wallet.monthly_balance == 28
    assert wallet.topup_balance == 25
    assert wallet.available_credits == 53

    # Double commit should be rejected
    with pytest.raises(ValueError):
        await engine.commit(res1.reservation_id, actual_amount=10)

    # 3. Reserve and Release (provider failure path)
    res2 = await engine.reserve(
        account_id=account_id,
        task_id="task-102",
        amount=20,
        idempotency_key="idemp-res-2",
    )
    assert wallet.available_credits == 33

    await engine.release(res2.reservation_id)
    await db_session.commit()
    await db_session.refresh(wallet)

    assert res2.status == ReservationStatus.RELEASED
    assert wallet.available_credits == 53

    # 4. Insufficient credits
    with pytest.raises(InsufficientCreditsError):
        await engine.reserve(
            account_id=account_id,
            task_id="task-huge",
            amount=9999,
        )


@pytest.mark.asyncio
async def test_append_only_credit_ledger_auditability(db_session):
    """
    Verifies that every mutation writes an append-only row to credit_transactions
    and historical records are never overwritten.
    """
    account = Account(email="audit_ledger@talos.dev", role="user")
    db_session.add(account)
    await db_session.commit()
    account_id = account.account_id

    engine = WalletEngine(db_session)
    await engine.create_wallet(account_id=account_id, initial_monthly=100, initial_topup=0)
    await db_session.commit()

    # Initial grant transaction
    tx_grant = CreditTransaction(
        account_id=account_id,
        type=TransactionType.subscription_grant,
        amount=100,
        note="Initial subscription grant",
        reference_id="grant-001",
    )
    db_session.add(tx_grant)
    await db_session.commit()

    # Reserve 20 credits
    res = await engine.reserve(account_id, "task-aud-1", 20, idempotency_key="k1")
    # Commit 15 credits (refunds 5)
    await engine.commit(res.reservation_id, actual_amount=15)
    await db_session.commit()

    # Check ledger rows
    stmt = select(CreditTransaction).where(CreditTransaction.account_id == account_id).order_by(CreditTransaction.created_at)
    txs = list((await db_session.execute(stmt)).scalars().all())

    # We expect: grant (+100), relay spend (-15)
    assert len(txs) == 2
    assert txs[0].amount == 100
    assert txs[1].amount == -15
    total_ledger = sum(tx.amount for tx in txs)
    wallet = await engine.get_wallet(account_id)
    assert total_ledger == wallet.available_credits == 85


@pytest.mark.asyncio
async def test_concurrent_credit_reservations(db_session):
    """
    Simulates concurrent reservation attempts to ensure atomic debit and prevent overspending.
    """
    account = Account(email="concurrent_test@talos.dev", role="user")
    db_session.add(account)
    await db_session.commit()
    account_id = account.account_id

    engine = WalletEngine(db_session)
    await engine.create_wallet(account_id=account_id, initial_monthly=25, initial_topup=0)
    await db_session.commit()

    # Attempt 5 concurrent reservations of 10 credits each.
    # With only 25 credits available, exactly 2 must succeed and 3 must raise InsufficientCreditsError.
    successes = []
    failures = []

    async def _try_reserve(task_id: str, key: str):
        try:
            r = await engine.reserve(account_id, task_id, amount=10, idempotency_key=key)
            successes.append(r)
        except InsufficientCreditsError as e:
            failures.append(e)

    await asyncio.gather(
        _try_reserve("task-c1", "k-c1"),
        _try_reserve("task-c2", "k-c2"),
        _try_reserve("task-c3", "k-c3"),
        _try_reserve("task-c4", "k-c4"),
        _try_reserve("task-c5", "k-c5"),
    )

    assert len(successes) == 2
    assert len(failures) == 3

    wallet = await engine.get_wallet(account_id)
    assert wallet.available_credits == 5


@pytest.mark.asyncio
async def test_stripe_customer_and_billing_transaction(db_session):
    """
    Verifies StripeCustomer mapping and BillingTransaction recording with unique constraints.
    """
    account = Account(email="stripe_rec@talos.dev", role="user")
    db_session.add(account)
    await db_session.commit()
    account_id = account.account_id

    # Customer mapping
    cust = StripeCustomer(
        account_id=account_id,
        stripe_customer_id="cus_test_12345",
    )
    db_session.add(cust)
    await db_session.commit()

    # Duplicate stripe_customer_id must fail
    dup_cust = StripeCustomer(
        account_id=uuid.uuid4(),
        stripe_customer_id="cus_test_12345",
    )
    db_session.add(dup_cust)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # Billing transaction recording
    tx = BillingTransaction(
        gateway="stripe",
        canonical_reference_id="ch_test_99999",
        account_id=account_id,
        amount_minor=1000,
        currency="usd",
        credits_granted=100,
        event_type="checkout.session.completed",
        payment_type="topup",
        status="processed",
    )
    db_session.add(tx)
    await db_session.commit()

    # Duplicate payment transaction with same canonical reference must fail
    dup_tx = BillingTransaction(
        gateway="stripe",
        canonical_reference_id="ch_test_99999",
        account_id=account_id,
        amount_minor=1000,
        currency="usd",
        credits_granted=100,
        event_type="checkout.session.completed",
    )
    db_session.add(dup_tx)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_durable_webhook_idempotency(db_session):
    """
    Tests that duplicate Stripe webhook deliveries are safely deduplicated
    in the database and do not double-credit the account.
    """
    account = Account(email="webhook_dedup@talos.dev", role="user")
    db_session.add(account)
    await db_session.commit()

    engine = WalletEngine(db_session)
    await engine.create_wallet(account_id=account.account_id, initial_monthly=0, initial_topup=0)
    await db_session.commit()

    event_id = f"evt_durable_{uuid.uuid4().hex}"
    event = {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "mode": "payment",
                "client_reference_id": str(account.account_id),
                "metadata": {"credits": "250"},
                "amount_total": 2500,
                "currency": "usd",
            }
        },
    }

    # 1. First webhook delivery
    res1 = await process_stripe_webhook_event(event, db=db_session)
    assert res1["status"] == "processed"
    assert res1["credits"] == 250

    # Wallet should have 250 topup credits
    wallet = await engine.get_wallet(account.account_id)
    assert wallet.available_credits == 250

    # WebhookEvent record exists and is marked processed
    wb = (await db_session.execute(select(WebhookEvent).where(WebhookEvent.event_id == event_id))).scalars().first()
    assert wb is not None
    assert wb.processed is True

    # BillingTransaction record exists
    bt = (await db_session.execute(select(BillingTransaction).where(BillingTransaction.canonical_reference_id == event_id))).scalars().first()
    assert bt is not None
    assert bt.amount_minor == 2500

    # 2. Second webhook delivery (simulating Stripe retry)
    res2 = await process_stripe_webhook_event(event, db=db_session)
    assert res2["status"] == "duplicate_ignored"

    # Wallet MUST STILL have 250 credits (NOT 500)
    await db_session.refresh(wallet)
    assert wallet.available_credits == 250


@pytest.mark.asyncio
async def test_marketplace_version_metadata_and_integrity(db_session):
    """
    Verifies that package versions store storage_key, sha256 checksum,
    permissions, requirements, compatibility, and enforce immutability.
    """
    author = Account(email="pkg_author@talos.dev", role="user")
    db_session.add(author)
    await db_session.commit()

    listing = MarketplaceListing(
        author_account_id=author.account_id,
        author_username="author",
        kind="skill",
        slug="web-research",
        display_name="Web Research",
        version="1.0.0",
        status="approved",
    )
    db_session.add(listing)
    await db_session.commit()

    v1 = MarketplacePackageVersion(
        listing_id=listing.listing_id,
        version="1.0.0",
        storage_key="skills/web-research/1.0.0/package.zip",
        bucket="talos-marketplace",
        file_size=12345,
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        permissions={"network": ["https://*"], "env": ["SEARCH_API_KEY"]},
        requirements={"python": ">=3.11", "packages": ["httpx>=0.27.0"]},
        compatibility={"talos": ">=5.0.0"},
        status="approved",
    )
    db_session.add(v1)
    await db_session.commit()
    await db_session.refresh(v1)

    assert v1.permissions["network"] == ["https://*"]
    assert v1.requirements["python"] == ">=3.11"
    assert v1.compatibility["talos"] == ">=5.0.0"
    assert v1.sha256.startswith("e3b0c442")

    # Immutability: attempting to publish identical version 1.0.0 must fail
    v1_dup = MarketplacePackageVersion(
        listing_id=listing.listing_id,
        version="1.0.0",
        storage_key="skills/web-research/1.0.0/package.zip",
        bucket="talos-marketplace",
        file_size=54321,
        sha256="abcdef1234567890",
    )
    db_session.add(v1_dup)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_usage_event_metering_and_credits_audit(db_session):
    """
    Verifies that UsageEvent records token consumption, model details internally,
    and credits charged for transparent auditing without leaking provider data.
    """
    account = Account(email="usage_user@talos.dev", role="user")
    db_session.add(account)
    await db_session.commit()

    event = UsageEvent(
        task_id="task-meter-01",
        account_id=account.account_id,
        capability_id="reasoning_model",
        input_tokens=1500,
        output_tokens=300,
        cached_tokens=100,
        reasoning_tokens=50,
        provider="anthropic",
        model_id="claude-3-7-sonnet-20250219",
        unit_type=UnitType.INPUT_TOKENS,
        quantity=1500,
        credits_charged=15,
        status="COMPLETED",
    )
    db_session.add(event)
    await db_session.commit()
    await db_session.refresh(event)

    assert event.credits_charged == 15
    assert event.input_tokens == 1500
    assert event.provider == "anthropic"
    assert event.task_id == "task-meter-01"
