"""
Tests for Two-Phase Installation & Pure-Read Download (Phase 6 Canonical):
  - Pure read GET /api/v1/marketplace/download/{publisher}/{slug} has zero side-effects
  - POST /api/v1/marketplace/installs/init creates single-use token without incrementing install_count
  - POST /api/v1/marketplace/installs/complete verifies token and atomically increments install_count
  - DELETE /api/v1/marketplace/installs/{listing_id} decrements install_count cleanly
  - GET /api/v1/marketplace/installs lists active installations
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
    UserInstall,
)
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
async def test_pure_read_download_has_no_side_effects(db_session, fake_storage):
    # Setup publisher and listing
    author = Account(account_id=uuid.uuid4(), email=f"pub_{uuid.uuid4().hex[:8]}@talos.dev", role="user")
    db_session.add(author)
    await db_session.flush()

    listing = MarketplaceListing(
        listing_id=uuid.uuid4(),
        author_account_id=author.account_id,
        publisher_slug="acme",
        author_username="acme",
        kind="tool",
        slug="grep-tool",
        display_name="Grep Tool",
        status="approved",
        version="1.0.0",
        install_count=0,
    )
    db_session.add(listing)
    await db_session.flush()

    # Add version in Tigris S3
    canonical_key = "tools/acme/grep-tool/1.0.0/package.zip"
    await fake_storage.upload(canonical_key, b"fake-zip-data", "application/zip")

    ver = MarketplacePackageVersion(
        version_id=uuid.uuid4(),
        listing_id=listing.listing_id,
        version="1.0.0",
        storage_key=canonical_key,
        bucket="talos-marketplace",
        file_size=len(b"fake-zip-data"),
        sha256="abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234",
        status="published",
    )
    db_session.add(ver)
    await db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET download URL
        res = await client.get("/api/v1/marketplace/download/acme/grep-tool", follow_redirects=False)
        assert res.status_code == 307
        assert "t3.storage.dev" in res.headers["location"] or "download_url" in res.headers["location"]

        # Invariant check: install_count MUST NOT change!
        await db_session.refresh(listing)
        assert listing.install_count == 0

        # No UserInstall records created
        inst_stmt = select(UserInstall).where(UserInstall.listing_id == listing.listing_id)
        inst_records = (await db_session.execute(inst_stmt)).scalars().all()
        assert len(inst_records) == 0

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_two_phase_install_and_uninstall_lifecycle(db_session, fake_storage):
    # Setup publisher, user, and listing
    publisher = Account(account_id=uuid.uuid4(), email=f"pub_{uuid.uuid4().hex[:8]}@talos.dev", role="user")
    user = Account(account_id=uuid.uuid4(), email=f"user_{uuid.uuid4().hex[:8]}@talos.dev", role="user")
    db_session.add_all([publisher, user])
    await db_session.flush()

    listing = MarketplaceListing(
        listing_id=uuid.uuid4(),
        author_account_id=publisher.account_id,
        publisher_slug="coder",
        author_username="coder",
        kind="agent",
        slug="coder-agent",
        display_name="Coder Agent",
        status="approved",
        version="2.0.0",
        install_count=5,
    )
    db_session.add(listing)
    await db_session.flush()

    canonical_key = "agents/coder/coder-agent/2.0.0/package.zip"
    await fake_storage.upload(canonical_key, b"fake-agent-zip", "application/zip")

    ver = MarketplacePackageVersion(
        version_id=uuid.uuid4(),
        listing_id=listing.listing_id,
        version="2.0.0",
        storage_key=canonical_key,
        bucket="talos-marketplace",
        file_size=len(b"fake-agent-zip"),
        sha256="1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
        status="published",
    )
    db_session.add(ver)
    await db_session.commit()

    # User logged in
    app.dependency_overrides[get_db] = lambda: db_session
    from app.api.v1.marketplace import require_account, get_optional_account
    app.dependency_overrides[require_account] = lambda: user
    app.dependency_overrides[get_optional_account] = lambda: user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Phase 1: Init Install
        init_res = await client.post(
            "/api/v1/marketplace/installs/init",
            json={
                "listing_id": str(listing.listing_id),
                "version": "2.0.0",
            },
        )
        assert init_res.status_code == 200, init_res.text
        init_data = init_res.json()
        assert "install_token" in init_data
        assert "download_url" in init_data
        assert init_data["sha256"] == ver.sha256
        install_token = init_data["install_token"]

        # Invariant: install_count MUST NOT increase in Phase 1
        await db_session.refresh(listing)
        assert listing.install_count == 5

        # Phase 2: Complete Install (client verified SHA-256 and successfully extracted)
        comp_res = await client.post(
            "/api/v1/marketplace/installs/complete",
            json={
                "listing_id": str(listing.listing_id),
                "install_token": install_token,
            },
        )
        assert comp_res.status_code == 200, comp_res.text
        comp_data = comp_res.json()
        assert comp_data["status"] == "active"

        # Invariant: install_count MUST be incremented now!
        await db_session.refresh(listing)
        assert listing.install_count == 6

        # Check list of user installs
        list_res = await client.get("/api/v1/marketplace/installs")
        assert list_res.status_code == 200
        installs = list_res.json()
        assert len(installs) == 1
        assert installs[0]["listing_id"] == str(listing.listing_id)
        assert installs[0]["version"] == "2.0.0"

        # Uninstall
        uninst_res = await client.delete(f"/api/v1/marketplace/installs/{listing.listing_id}")
        assert uninst_res.status_code == 200
        assert uninst_res.json()["ok"] is True

        # Invariant: install_count MUST decrement back
        await db_session.refresh(listing)
        assert listing.install_count == 5

    app.dependency_overrides.clear()
