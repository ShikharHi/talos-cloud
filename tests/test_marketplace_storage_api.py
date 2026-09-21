"""
Integration tests for Marketplace S3 Storage Endpoints.

Tests full lifecycle:
  Init Upload -> Fake S3 Staging -> Complete Upload -> Verification & Promotion -> Download URL
Also verifies immutability constraints, authorization checks, and asset flows.
"""

import io
import uuid
import zipfile
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db
from app.main import app
from app.models.accounts import Account
from app.models.marketplace import MarketplaceListing, MarketplacePackageVersion, PackageUpload
from app.storage.models import UploadState, VersionState
from app.storage.service import StorageService, reset_storage_service
from tests.test_storage_provider import FakeStorageProvider


def _make_valid_agent_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("agent.yaml", "name: test-bot\nauthor: alice\nversion: 1.0.0\nkind: agent\n")
        zf.writestr("agent.py", "print('hello bot')\n")
    return buf.getvalue()


@pytest.fixture
def fake_storage(monkeypatch):
    """Sets up a FakeStorageProvider for StorageService during tests."""
    reset_storage_service()
    provider = FakeStorageProvider(default_bucket="talos-marketplace")
    service = StorageService(provider=provider, default_bucket="talos-marketplace")
    monkeypatch.setattr("app.routers.marketplace.get_storage_service", lambda: service)
    monkeypatch.setattr("app.storage.service._storage_service_instance", service)
    return provider


@pytest.mark.asyncio
async def test_full_package_upload_and_download_flow(db_session, fake_storage):
    """
    Tests end-to-end package upload, completion, verification, promotion, and download.
    """
    # 1. Setup author account
    author = Account(email="alice@example.com", role="user")
    db_session.add(author)
    await db_session.commit()
    await db_session.refresh(author)

    # Dependency override for authenticated user
    app.dependency_overrides[get_db] = lambda: db_session
    from app.routers.marketplace import get_optional_account
    app.dependency_overrides[get_optional_account] = lambda: author

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 2. Upload Init
        init_res = await client.post(
            "/marketplace/agents/alice-agent/versions/1.0.0/upload/init",
            json={"file_size": 500},
        )
        assert init_res.status_code == 200, init_res.text
        init_data = init_res.json()
        assert "upload_id" in init_data
        assert "upload_url" in init_data
        upload_id = init_data["upload_id"]
        object_key = init_data["object_key"]

        # Check DB record
        upload_rec = (await db_session.execute(
            select(PackageUpload).where(PackageUpload.upload_id == uuid.UUID(upload_id))
        )).scalars().first()
        assert upload_rec is not None
        assert upload_rec.status == UploadState.PENDING.value

        # 3. Simulate Client Direct PUT to S3 Staging Key
        zip_bytes = _make_valid_agent_zip()
        await fake_storage.upload(object_key, zip_bytes, "application/zip")
        assert await fake_storage.exists(object_key) is True

        # 4. Upload Complete (Server Verification & Promotion)
        complete_res = await client.post(
            "/marketplace/agents/alice-agent/versions/1.0.0/upload/complete",
            json={
                "upload_id": upload_id,
                "display_name": "Alice Agent",
                "tagline": "A smart assistant",
            },
        )
        assert complete_res.status_code == 200, complete_res.text
        ver_data = complete_res.json()
        assert ver_data["version"] == "1.0.0"
        assert ver_data["status"] == VersionState.APPROVED.value
        assert len(ver_data["sha256"]) == 64

        # Verify Canonical Storage Promotion
        canonical_key = "agents/alice-agent/1.0.0/package.zip"
        assert await fake_storage.exists(canonical_key) is True
        # Verify Staging Key Cleanup
        assert await fake_storage.exists(object_key) is False

        # 5. Download URL Generation
        dl_res = await client.get("/marketplace/agents/alice-agent/versions/1.0.0/download")
        assert dl_res.status_code == 200, dl_res.text
        dl_data = dl_res.json()
        assert "download_url" in dl_data
        assert "agents/alice-agent/1.0.0/package.zip" in dl_data["download_url"]

        # 6. Immutability Violation Test:
        # Attempting to init upload for the SAME version 1.0.0 must be rejected with 409 Conflict
        dup_init = await client.post(
            "/marketplace/agents/alice-agent/versions/1.0.0/upload/init",
            json={},
        )
        assert dup_init.status_code == 409
        assert "immutable" in dup_init.json()["detail"].lower()

        # 7. Backward-Compatible Download Endpoint:
        # /marketplace/download/alice/alice-agent should redirect (307) to the Tigris download URL
        compat_dl = await client.get("/marketplace/download/alice/alice-agent", follow_redirects=False)
        assert compat_dl.status_code == 307
        assert "download_url" in compat_dl.headers["location"] or "t3.storage.dev" in compat_dl.headers["location"]

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_asset_upload_and_download_flow(db_session, fake_storage):
    """
    Tests asset upload initialization, staging, promotion, and download URL generation.
    """
    author = Account(email="bob@example.com", role="user")
    db_session.add(author)
    await db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    from app.routers.marketplace import get_optional_account
    app.dependency_overrides[get_optional_account] = lambda: author

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Asset upload init
        init_res = await client.post(
            "/marketplace/skills/summarizer/assets/icon.png/upload/init",
            json={"filename": "icon.png", "content_type": "image/png"},
        )
        assert init_res.status_code == 200
        init_data = init_res.json()
        upload_id = init_data["upload_id"]
        staging_key = init_data["object_key"]

        # 2. Simulate client direct PUT
        await fake_storage.upload(staging_key, b"fake-png-content", "image/png")

        # 3. Complete asset upload
        comp_res = await client.post(
            "/marketplace/skills/summarizer/assets/icon.png/upload/complete",
            json={"upload_id": upload_id, "filename": "icon.png"},
        )
        assert comp_res.status_code == 200
        canonical_key = "assets/skills/summarizer/icon.png"
        assert await fake_storage.exists(canonical_key) is True

        # 4. Download asset URL
        dl_res = await client.get("/marketplace/skills/summarizer/assets/icon.png/download")
        assert dl_res.status_code == 200
        assert "download_url" in dl_res.json()

    app.dependency_overrides.clear()
