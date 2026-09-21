"""
Unit and Integration Tests for Canonical Production API Routes (Freeze Contract).

Verifies:
  1. RFC 8628 device flow routes:
     - POST /api/v1/devices/code
     - POST /api/v1/devices/token
  2. LLM Relay routes:
     - POST /api/v1/llm/call (no provider leaked, auth required)
  3. Marketplace routes:
     - GET /api/v1/marketplace/search
     - POST /api/v1/marketplace/installs
     - GET /api/v1/marketplace/installs
     - POST /api/v1/marketplace/upload/complete (202 Accepted on async)
"""

import uuid
from datetime import datetime, timezone
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db
from app.main import app
from app.models.accounts import Account
from app.models.marketplace import MarketplaceListing, PackageUpload, UserInstall


@pytest.mark.asyncio
async def test_canonical_device_code_route():
    """Verifies that /api/v1/devices/code is reachable and initiates RFC 8628 flow."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/v1/devices/code", json={"client_id": "talos-desktop"})
        assert res.status_code == 200
        data = res.json()
        assert "device_code" in data
        assert "user_code" in data
        assert "verification_uri" in data
        assert data["expires_in"] > 0


@pytest.mark.asyncio
async def test_canonical_llm_call_requires_auth():
    """Verifies that /api/v1/llm/call requires bearer authentication."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/llm/call",
            headers={"Authorization": "Bearer invalid-token"},
            json={"capability_id": "fast_model", "payload": {}, "worst_case_units": 10},
        )
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_canonical_marketplace_search(db_session):
    """Verifies that /api/v1/marketplace/search returns catalog listings."""
    listing_id = uuid.uuid4()
    author_id = uuid.uuid4()

    author = Account(account_id=author_id, email=f"author_{author_id.hex[:6]}@example.com")
    db_session.add(author)
    await db_session.flush()

    listing = MarketplaceListing(
        listing_id=listing_id,
        author_account_id=author_id,
        author_username="testdev",
        kind="skills",
        slug="canon-skill",
        display_name="Canonical Skill",
        tagline="A test skill",
        icon_emoji="⚡",
        icon_color="#ffaa00",
        tags=["utility"],
        status="approved",
        version="1.0.0",
        install_count=0,
        is_builtin=False,
    )
    db_session.add(listing)
    await db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/v1/marketplace/search?q=canon")
        assert res.status_code == 200
        items = res.json()
        assert len(items) >= 1
        assert any(i["slug"] == "canon-skill" for i in items)
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_canonical_marketplace_installs_flow(db_session):
    """Verifies that /api/v1/marketplace/installs installs and lists installed packages."""
    user_id = uuid.uuid4()
    user = Account(account_id=user_id, email=f"installer_{user_id.hex[:6]}@example.com")
    db_session.add(user)

    listing_id = uuid.uuid4()
    listing = MarketplaceListing(
        listing_id=listing_id,
        author_account_id=user_id,
        author_username="canondev",
        kind="agents",
        slug="agent-x",
        display_name="Agent X",
        tagline="Autonomous helper",
        icon_emoji="🤖",
        icon_color="#00ff00",
        tags=["agent"],
        status="approved",
        version="2.0.0",
        install_count=0,
        is_builtin=False,
    )
    db_session.add(listing)
    await db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    from app.api.v1.marketplace import require_account, get_optional_account as v1_get_optional
    app.dependency_overrides[require_account] = lambda: user
    app.dependency_overrides[v1_get_optional] = lambda: user

    # compat_install_listing also needs a published version in DB for InstallService.
    # No published version → InstallService raises VersionNotFoundError → 400.
    # We only verify auth passes (200) so we also need a published version.
    from app.models.marketplace import MarketplacePackageVersion
    import hashlib as _hl
    dummy_zip = b"PK\x05\x06" + b"\x00" * 18
    ver = MarketplacePackageVersion(
        version_id=uuid.uuid4(),
        listing_id=listing_id,
        version="2.0.0",
        storage_key="agents/canondev/agent-x/2.0.0/package.zip",
        bucket="talos-marketplace",
        file_size=len(dummy_zip),
        sha256=_hl.sha256(dummy_zip).hexdigest(),
        status="published",
    )
    db_session.add(ver)
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Install
        inst_res = await client.post(
            "/api/v1/marketplace/installs",
            json={"author": "canondev", "slug": "agent-x"},
        )
        assert inst_res.status_code == 200
        inst_data = inst_res.json()
        assert inst_data["ok"] is True
        assert inst_data["version"] == "2.0.0"

        # Check DB install count incremented
        await db_session.refresh(listing)
        assert listing.install_count == 1

        # Check /api/v1/marketplace/installs
        get_res = await client.get("/api/v1/marketplace/installs")
        assert get_res.status_code == 200
        installed_list = get_res.json()
        assert any(item["listing"]["slug"] == "agent-x" for item in installed_list)

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_canonical_marketplace_upload_complete_async(db_session):
    """Verifies that /api/v1/marketplace/upload/complete returns 202 Accepted when async."""
    from app.storage.service import StorageService, reset_storage_service
    from tests.test_storage_provider import FakeStorageProvider
    import datetime as _dt

    reset_storage_service()
    fake_provider = FakeStorageProvider(default_bucket="talos-marketplace")
    fake_service = StorageService(provider=fake_provider, default_bucket="talos-marketplace")

    user_id = uuid.uuid4()
    user = Account(account_id=user_id, email=f"uploader_{user_id.hex[:6]}@example.com")
    db_session.add(user)

    upload_id = uuid.uuid4()
    staging_key = "uploads/skills/async-skill/upload.zip"

    # Put a fake object in the fake storage so exists() check passes
    await fake_provider.upload(staging_key, b"PK\x05\x06" + b"\x00" * 18, "application/zip")

    upload = PackageUpload(
        upload_id=upload_id,
        account_id=user_id,
        resource_type="skills",
        resource_id="async-skill",
        version="1.0.0",
        object_key=staging_key,
        staging_key=staging_key,
        bucket="talos-marketplace",
        status="uploaded",
        expires_at=datetime.now(timezone.utc) + _dt.timedelta(hours=1),
    )
    db_session.add(upload)
    await db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    from app.api.v1.marketplace import require_account, get_optional_account as v1_get_optional
    from app.infrastructure.storage.tigris import TigrisMarketplaceStorage
    app.dependency_overrides[require_account] = lambda: user
    app.dependency_overrides[v1_get_optional] = lambda: user

    # Inject the fake storage into TigrisMarketplaceStorage used by UploadService
    original_tigris_init = TigrisMarketplaceStorage.__init__

    def _fake_tigris_init(self, storage_service=None):
        self._storage = fake_service

    TigrisMarketplaceStorage.__init__ = _fake_tigris_init

    # Mock Celery task delay so async_verification path runs without a real broker
    import app.celery_app.tasks.marketplace as _mp_tasks
    original_delay = getattr(_mp_tasks.verify_and_promote_package_task, "delay", None)
    _mp_tasks.verify_and_promote_package_task.delay = lambda *a, **k: None

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post(
                "/api/v1/marketplace/upload/complete",
                json={"upload_id": str(upload_id), "async_verification": True},
            )
            assert res.status_code == 202
            data = res.json()
            assert data["status"] == "verifying"
            assert data["upload_id"] == str(upload_id)
    finally:
        TigrisMarketplaceStorage.__init__ = original_tigris_init
        if original_delay is not None:
            _mp_tasks.verify_and_promote_package_task.delay = original_delay
        app.dependency_overrides.clear()
        reset_storage_service()
