"""
Tests for Marketplace Compatibility Layer:
  - Legacy /marketplace/listings search and retrieval
  - Legacy /marketplace/listings/{author}/{slug}
  - Legacy /marketplace/install/{author}/{slug}
  - Legacy /marketplace/installed
  - Legacy /marketplace/download/{author}/{slug}
  - Canonical compat aliases: /api/v1/marketplace/search, /api/v1/marketplace/items/{author}/{slug}
  - Direct single-step /api/v1/marketplace/installs fallback
"""

import uuid
import pytest
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.models.accounts import Account
from app.models.marketplace import MarketplaceListing, MarketplacePackageVersion
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
async def test_legacy_marketplace_routes_and_aliases(db_session, fake_storage):
    # Setup test accounts
    author = Account(account_id=uuid.uuid4(), email=f"compat_author_{uuid.uuid4().hex[:8]}@talos.dev", role="user")
    consumer = Account(account_id=uuid.uuid4(), email=f"compat_user_{uuid.uuid4().hex[:8]}@talos.dev", role="user")
    db_session.add_all([author, consumer])
    await db_session.flush()

    listing = MarketplaceListing(
        listing_id=uuid.uuid4(),
        author_account_id=author.account_id,
        publisher_slug="compat-author",
        author_username="compat-author",
        kind="skill",
        slug="weather-skill",
        display_name="Weather Skill",
        tagline="Get weather updates",
        status="approved",
        version="1.0.0",
        install_count=0,
        tags=["skill", "weather"],
    )
    db_session.add(listing)
    await db_session.flush()

    canonical_key = "skills/compat-author/weather-skill/1.0.0/package.zip"
    await fake_storage.upload(canonical_key, b"fake-weather-zip", "application/zip")

    ver = MarketplacePackageVersion(
        version_id=uuid.uuid4(),
        listing_id=listing.listing_id,
        version="1.0.0",
        storage_key=canonical_key,
        bucket="talos-marketplace",
        file_size=len(b"fake-weather-zip"),
        sha256="555566667777888899990000aaaabbbbccccddddeeeeffff0000111122223333",
        status="published",
    )
    db_session.add(ver)
    await db_session.commit()

    # User authenticated
    app.dependency_overrides[get_db] = lambda: db_session
    from app.routers.marketplace import get_optional_account as legacy_get_account
    from app.api.v1.marketplace import require_account, get_optional_account as v1_get_account

    app.dependency_overrides[legacy_get_account] = lambda: consumer
    app.dependency_overrides[require_account] = lambda: consumer
    app.dependency_overrides[v1_get_account] = lambda: consumer

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. GET /marketplace/listings
        listings_res = await client.get("/marketplace/listings")
        assert listings_res.status_code == 200
        items = listings_res.json()
        assert any(item["slug"] == "weather-skill" for item in items)

        # 2. GET /marketplace/listings/{author}/{slug}
        detail_res = await client.get("/marketplace/listings/compat-author/weather-skill")
        assert detail_res.status_code == 200
        detail = detail_res.json()
        assert detail["slug"] == "weather-skill"
        assert detail["display_name"] == "Weather Skill"

        # 3. GET /api/v1/marketplace/items/{author}/{slug} alias
        item_res = await client.get("/api/v1/marketplace/items/compat-author/weather-skill")
        assert item_res.status_code == 200
        assert item_res.json()["slug"] == "weather-skill"

        # 4. GET /api/v1/marketplace/search alias
        search_res = await client.get("/api/v1/marketplace/search?q=weather")
        assert search_res.status_code == 200
        search_items = search_res.json()
        assert len(search_items) >= 1
        assert search_items[0]["slug"] == "weather-skill"

        # 5. POST /marketplace/install/{author}/{slug} (Legacy direct install)
        inst_res = await client.post("/marketplace/install/compat-author/weather-skill")
        assert inst_res.status_code == 200
        assert inst_res.json()["ok"] is True

        await db_session.refresh(listing)
        assert listing.install_count == 1

        # 6. GET /marketplace/installed
        installed_res = await client.get("/marketplace/installed")
        assert installed_res.status_code == 200
        installed = installed_res.json()
        assert any(i["slug"] == "weather-skill" for i in installed)

        # 7. GET /marketplace/download/{author}/{slug} (Legacy download redirect)
        dl_res = await client.get("/marketplace/download/compat-author/weather-skill", follow_redirects=False)
        assert dl_res.status_code == 307
        assert "t3.storage.dev" in dl_res.headers["location"] or "download_url" in dl_res.headers["location"]

        # 8. DELETE /marketplace/install/{author}/{slug} (Legacy uninstall)
        uninst_res = await client.delete("/marketplace/install/compat-author/weather-skill")
        assert uninst_res.status_code == 200
        assert uninst_res.json()["ok"] is True

        await db_session.refresh(listing)
        assert listing.install_count == 0

        # 9. POST /api/v1/marketplace/installs (Single-step direct install compat)
        compat_inst_res = await client.post(
            "/api/v1/marketplace/installs",
            json={
                "listing_id": str(listing.listing_id),
                "author": "compat-author",
                "slug": "weather-skill",
            },
        )
        assert compat_inst_res.status_code == 200
        assert compat_inst_res.json()["ok"] is True

        await db_session.refresh(listing)
        assert listing.install_count == 1

    app.dependency_overrides.clear()
