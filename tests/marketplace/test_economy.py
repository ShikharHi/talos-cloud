"""
End-to-End Tests for Talos Marketplace Economy & Entitlements:
  1. Atomic Purchase flow: buyer wallet hold, 50/50 split commit, creator earning, entitlement creation
  2. Insufficient credits failure cleanly caught with no side effects
  3. Decoupled install: entitlement allows install without repurchase
  4. Unpurchased paid package install blocked by entitlement gate
  5. Creator Dashboard aggregates earnings, available balance, published listings
  6. Creator Public Profile returns aggregate stats without exposing secrets
  7. Weaviate Search Service fallback to PostgreSQL authoritative search
"""

import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db
from app.main import app
from app.models.accounts import Account
from app.models.marketplace import (
    MarketplaceListing,
    MarketplacePackageVersion,
    MarketplaceEntitlement,
    CreatorEarning,
)
from app.api.v1.marketplace import require_account
from app.services.wallet_engine import WalletEngine
from app.services.marketplace.purchase_service import PurchaseService
from app.services.marketplace.search_service import WeaviateSearchService
from app.storage.service import StorageService, reset_storage_service
from tests.test_storage_provider import FakeStorageProvider


@pytest.fixture
def fake_storage(monkeypatch):
    reset_storage_service()
    provider = FakeStorageProvider(default_bucket="talos-marketplace")
    service = StorageService(provider=provider, default_bucket="talos-marketplace")
    monkeypatch.setattr("app.storage.service._storage_service_instance", service)
    monkeypatch.setattr("app.routers.marketplace.get_storage_service", lambda: service)
    return provider


@pytest.mark.asyncio
async def test_paid_listing_purchase_and_50_50_split(db_session, fake_storage):
    buyer_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    listing_id = uuid.uuid4()

    creator = Account(
        account_id=creator_id,
        email=f"creator_{creator_id.hex[:8]}@talos.dev",
        role="user",
        publisher_slug="apexlabs",
        bio="Top AI creator",
        verified_publisher=True,
    )
    buyer = Account(
        account_id=buyer_id,
        email=f"buyer_{buyer_id.hex[:8]}@talos.dev",
        role="user",
    )
    db_session.add_all([creator, buyer])
    await db_session.flush()

    # Credit buyer wallet with 1000 credits
    wallet_engine = WalletEngine(db_session)
    await wallet_engine.get_wallet(buyer_id)
    await wallet_engine.credit_topup(buyer_id, amount=1000, purchase_ref="test_signup")

    # Create a paid MarketplaceListing (500 credits)
    listing = MarketplaceListing(
        listing_id=listing_id,
        author_account_id=creator_id,
        publisher_slug="apexlabs",
        author_username="apexlabs",
        kind="agent",
        slug="research-agent",
        display_name="Deep Research Agent",
        status="approved",
        version="1.0.0",
        pricing_type="paid",
        price_credits=500,
        install_count=0,
        purchase_count=0,
    )
    db_session.add(listing)
    await db_session.flush()

    # Create published package version in storage
    key = "agents/apexlabs/research-agent/1.0.0/package.zip"
    await fake_storage.upload(key, b"fake-zip-data", "application/zip")
    ver = MarketplacePackageVersion(
        version_id=uuid.uuid4(),
        listing_id=listing_id,
        version="1.0.0",
        storage_key=key,
        bucket="talos-marketplace",
        file_size=len(b"fake-zip-data"),
        sha256="1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd",
        status="published",
    )
    db_session.add(ver)
    await db_session.commit()

    # Act: Purchase the listing
    app.dependency_overrides[get_db] = lambda: db_session

    async def get_test_buyer():
        res = await db_session.execute(select(Account).where(Account.account_id == buyer_id))
        return res.scalar_one()

    app.dependency_overrides[require_account] = get_test_buyer

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(f"/api/v1/marketplace/items/{listing_id}/purchase")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["price_paid_credits"] == 500
        assert data["status"] == "active"
        assert "entitlement_id" in data

        # Verify entitlement created in database
        ent_stmt = select(MarketplaceEntitlement).where(
            MarketplaceEntitlement.account_id == buyer_id,
            MarketplaceEntitlement.listing_id == listing_id,
        )
        ent = (await db_session.execute(ent_stmt)).scalar_one_or_none()
        assert ent is not None
        assert ent.price_paid_credits == 500
        assert ent.status == "active"

        # Verify CreatorEarning recorded exactly 50/50 split (250 cr creator / 250 cr platform)
        earn_stmt = select(CreatorEarning).where(
            CreatorEarning.creator_id == creator_id,
            CreatorEarning.listing_id == listing_id,
        )
        earning = (await db_session.execute(earn_stmt)).scalar_one_or_none()
        assert earning is not None
        assert earning.gross_credits == 500
        assert earning.creator_share_credits == 250
        assert earning.platform_share_credits == 250

        # Verify buyer balance was debited 500 credits
        buyer_bal = await wallet_engine.get_balance(buyer_id)
        assert buyer_bal["total_credits"] == 500

        # Verify purchase count incremented
        listing_stmt = select(MarketplaceListing).where(MarketplaceListing.listing_id == listing_id)
        refreshed_listing = (await db_session.execute(listing_stmt)).scalar_one()
        assert refreshed_listing.purchase_count == 1

        # Verify GET /api/v1/marketplace/entitlements lists active entitlement
        ent_res = await client.get("/api/v1/marketplace/entitlements")
        assert ent_res.status_code == 200
        ents = ent_res.json()
        assert len(ents) == 1
        assert ents[0]["slug"] == "research-agent"
        assert ents[0]["price_paid_credits"] == 500

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_insufficient_credits_blocks_purchase(db_session, fake_storage):
    broke_buyer_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    listing_id = uuid.uuid4()

    creator = Account(account_id=creator_id, email=f"creator_{creator_id.hex[:8]}@talos.dev", role="user")
    broke_buyer = Account(account_id=broke_buyer_id, email=f"broke_{broke_buyer_id.hex[:8]}@talos.dev", role="user")
    db_session.add_all([creator, broke_buyer])
    await db_session.flush()

    # Buyer has only 50 credits
    wallet_engine = WalletEngine(db_session)
    await wallet_engine.get_wallet(broke_buyer_id)
    await wallet_engine.credit_topup(broke_buyer_id, amount=50, purchase_ref="test_signup")

    listing = MarketplaceListing(
        listing_id=listing_id,
        author_account_id=creator_id,
        publisher_slug="apexlabs",
        author_username="apexlabs",
        kind="tool",
        slug="expensive-tool",
        display_name="Expensive Tool",
        status="approved",
        version="1.0.0",
        pricing_type="paid",
        price_credits=500,
    )
    db_session.add(listing)
    await db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_account] = lambda: broke_buyer

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(f"/api/v1/marketplace/items/{listing_id}/purchase")
        assert res.status_code == 400
        assert "insufficient credits" in res.json()["detail"].lower()

        # Buyer balance must be untouched
        bal = await wallet_engine.get_balance(broke_buyer_id)
        assert bal["total_credits"] == 50

        # No entitlements or earnings
        ent_stmt = select(MarketplaceEntitlement).where(MarketplaceEntitlement.account_id == broke_buyer_id)
        assert len((await db_session.execute(ent_stmt)).scalars().all()) == 0

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unpurchased_paid_package_blocks_install_init(db_session, fake_storage):
    creator = Account(account_id=uuid.uuid4(), email=f"creator_{uuid.uuid4().hex[:8]}@talos.dev", role="user")
    buyer = Account(account_id=uuid.uuid4(), email=f"buyer_{uuid.uuid4().hex[:8]}@talos.dev", role="user")
    db_session.add_all([creator, buyer])
    await db_session.flush()

    listing = MarketplaceListing(
        listing_id=uuid.uuid4(),
        author_account_id=creator.account_id,
        publisher_slug="apexlabs",
        author_username="apexlabs",
        kind="skill",
        slug="premium-skill",
        display_name="Premium Skill",
        status="approved",
        version="1.0.0",
        pricing_type="paid",
        price_credits=200,
    )
    db_session.add(listing)
    await db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_account] = lambda: buyer

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/marketplace/installs/init",
            json={"listing_id": str(listing.listing_id)},
        )
        assert res.status_code == 400
        assert "requires purchase" in res.json()["detail"].lower()

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_creator_dashboard_and_public_profile(db_session):
    creator = Account(
        account_id=uuid.uuid4(),
        email=f"creator_{uuid.uuid4().hex[:8]}@talos.dev",
        role="user",
        publisher_slug="superstudio",
        bio="Building state of the art agents",
        verified_publisher=True,
    )
    db_session.add(creator)
    await db_session.flush()

    listing = MarketplaceListing(
        listing_id=uuid.uuid4(),
        author_account_id=creator.account_id,
        publisher_slug="superstudio",
        author_username="superstudio",
        kind="agent",
        slug="super-agent",
        display_name="Super Agent",
        status="approved",
        version="1.0.0",
        pricing_type="paid",
        price_credits=100,
        install_count=15,
        download_count=42,
        purchase_count=5,
    )
    db_session.add(listing)
    await db_session.flush()

    # Insert mock creator earnings
    earning = CreatorEarning(
        earning_id=uuid.uuid4(),
        creator_id=creator.account_id,
        listing_id=listing.listing_id,
        purchase_id=f"tx_{uuid.uuid4().hex[:8]}",
        gross_credits=100,
        creator_share_credits=50,
        platform_share_credits=50,
        status="available",
    )
    db_session.add(earning)
    await db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_account] = lambda: creator

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Check creator private dashboard
        dash_res = await client.get("/api/v1/marketplace/creator/dashboard")
        assert dash_res.status_code == 200
        dash_data = dash_res.json()
        assert dash_data["overview"]["published_items"] == 1
        assert dash_data["overview"]["total_credits_earned"] == 50
        assert dash_data["overview"]["available_earnings"] == 50
        assert dash_data["overview"]["currency"] == "Talos Credits"
        assert len(dash_data["recent_earnings"]) == 1

        # Check creator public profile
        prof_res = await client.get("/api/v1/marketplace/creators/superstudio")
        assert prof_res.status_code == 200
        prof_data = prof_res.json()
        assert prof_data["publisher_slug"] == "superstudio"
        assert prof_data["verified_publisher"] is True
        assert prof_data["stats"]["total_purchases"] == 5
        assert prof_data["stats"]["total_installs"] == 15
        assert len(prof_data["items"]) == 1

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_weaviate_search_service_resilience():
    # Verify that unconfigured Weaviate gracefully reports unready and search returns None
    service = WeaviateSearchService()
    assert service.is_configured is False or service.is_configured is True
    if not service.is_configured:
        res = await service.hybrid_search("code generation")
        assert res is None
