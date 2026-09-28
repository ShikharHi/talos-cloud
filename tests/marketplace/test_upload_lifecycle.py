"""
Tests for Package Upload Lifecycle (Phase 6 Canonical):
  - Presigned upload initialization (/api/v1/marketplace/uploads/init)
  - Direct S3 staging and state transitions
  - Complete upload, scanner verification & canonical promotion
  - Version immutability (cannot overwrite published versions)
  - Publisher authorization enforcement
"""

import io
import uuid
import zipfile
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db
from app.main import app
from app.models.accounts import Account
from app.models.marketplace import MarketplaceListing, MarketplacePackageVersion, PackageUpload
from app.storage.service import StorageService, reset_storage_service
from tests.test_storage_provider import FakeStorageProvider


def _make_valid_skill_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SKILL.md", "---\nname: my-skill\ndescription: Test skill\nversion: 1.0.0\n---\n\n# Instructions\n")
        zf.writestr("run.py", "print('Skill ready')\n")
    return buf.getvalue()


@pytest.fixture
def fake_storage(monkeypatch):
    reset_storage_service()
    provider = FakeStorageProvider(default_bucket="talos-marketplace")
    service = StorageService(provider=provider, default_bucket="talos-marketplace")
    monkeypatch.setattr("app.storage.service._storage_service_instance", service)
    monkeypatch.setattr("app.routers.marketplace.get_storage_service", lambda: service)
    return provider


@pytest.mark.asyncio
async def test_canonical_upload_and_promotion_flow(db_session, fake_storage):
    # 1. Create Author and Listing
    author_email = f"alice_{uuid.uuid4().hex[:8]}@example.com"
    author = Account(account_id=uuid.uuid4(), email=author_email, role="user")
    db_session.add(author)
    await db_session.flush()

    await db_session.commit()

    # Override auth dependencies
    app.dependency_overrides[get_db] = lambda: db_session
    from app.api.v1.marketplace import require_account, get_optional_account
    app.dependency_overrides[require_account] = lambda: author
    app.dependency_overrides[get_optional_account] = lambda: author

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create a skill listing as a hidden draft until the uploaded archive passes scanning.
        listing_res = await client.post(
            "/api/v1/marketplace/listings",
            json={
                "publisher_slug": "alice",
                "slug": "text-summarizer",
                "kind": "skill",
                "display_name": "Text Summarizer",
                "manifest_yaml": "name: text-summarizer\ndescription: Test skill\n",
            },
        )
        assert listing_res.status_code == 201, listing_res.text
        listing_id = uuid.UUID(listing_res.json()["listing_id"])
        listing = (await db_session.execute(
            select(MarketplaceListing).where(MarketplaceListing.listing_id == listing_id)
        )).scalar_one()
        assert listing.status == "pending_review"
        hidden_res = await client.get("/api/v1/marketplace/listings")
        assert all(item["listing_id"] != str(listing.listing_id) for item in hidden_res.json())
        all_status_res = await client.get("/api/v1/marketplace/listings?status=all")
        assert all(item["listing_id"] != str(listing.listing_id) for item in all_status_res.json())
        hidden_detail_res = await client.get(f"/api/v1/marketplace/listings/{listing.listing_id}")
        assert hidden_detail_res.status_code == 404

        # A failed validation keeps the listing hidden but allows its owner to retry.
        bad_init_res = await client.post(
            "/api/v1/marketplace/uploads/init",
            json={"listing_id": str(listing.listing_id), "version": "1.0.1"},
        )
        bad_upload_id = bad_init_res.json()["upload_id"]
        bad_staging_key = bad_init_res.json()["staging_key"]
        await fake_storage.upload(bad_staging_key, b"not a zip archive", "application/zip")
        bad_complete_res = await client.post(
            "/api/v1/marketplace/uploads/complete",
            json={"upload_id": bad_upload_id, "async_verification": False},
        )
        assert bad_complete_res.json()["status"] == "failed"
        retry_listing_res = await client.post(
            "/api/v1/marketplace/listings",
            json={
                "publisher_slug": "alice",
                "slug": "text-summarizer",
                "kind": "skill",
                "display_name": "Text Summarizer",
                "manifest_yaml": "name: text-summarizer\ndescription: Test skill\n",
            },
        )
        assert retry_listing_res.status_code == 201
        assert retry_listing_res.json()["listing_id"] == str(listing.listing_id)

        # Init Upload
        init_res = await client.post(
            "/api/v1/marketplace/uploads/init",
            json={
                "listing_id": str(listing.listing_id),
                "version": "1.0.1",
                "file_size": 256,
            },
        )
        assert init_res.status_code == 200, init_res.text
        init_data = init_res.json()
        upload_id = init_data["upload_id"]
        staging_key = init_data["staging_key"]
        assert "upload_url" in init_data

        # Verify DB upload session is pending
        upload_stmt = select(PackageUpload).where(PackageUpload.upload_id == uuid.UUID(upload_id))
        upload_rec = (await db_session.execute(upload_stmt)).scalars().first()
        assert upload_rec is not None
        assert upload_rec.status == "pending"

        # 3. Simulate S3 client PUT
        zip_bytes = _make_valid_skill_zip()
        await fake_storage.upload(staging_key, zip_bytes, "application/zip")
        assert await fake_storage.exists(staging_key) is True

        # 4. Complete Upload (Synchronous verification)
        comp_res = await client.post(
            "/api/v1/marketplace/uploads/complete",
            json={
                "upload_id": upload_id,
                "async_verification": False,
            },
        )
        assert comp_res.status_code == 200, comp_res.text
        comp_data = comp_res.json()
        assert comp_data["status"] == "promoted"
        canonical_key = comp_data["canonical_key"]

        # Verify S3 promotion and staging cleanup
        assert await fake_storage.exists(canonical_key) is True
        assert await fake_storage.exists(staging_key) is False

        # Verify version record in DB
        ver_stmt = select(MarketplacePackageVersion).where(
            MarketplacePackageVersion.listing_id == listing.listing_id,
            MarketplacePackageVersion.version == "1.0.1",
        )
        ver_rec = (await db_session.execute(ver_stmt)).scalars().first()
        assert ver_rec is not None
        assert ver_rec.status == "published"
        assert ver_rec.sha256 != ""
        await db_session.refresh(listing)
        assert listing.status == "approved"
        visible_res = await client.get("/api/v1/marketplace/listings")
        assert any(item["listing_id"] == str(listing.listing_id) for item in visible_res.json())

        # 5. Immutability Violation: Attempting to re-upload version 1.0.1 must be 400 or 409
        dup_init = await client.post(
            "/api/v1/marketplace/uploads/init",
            json={
                "listing_id": str(listing.listing_id),
                "version": "1.0.1",
            },
        )
        assert dup_init.status_code == 400
        assert "immutable" in dup_init.json()["detail"].lower()

        unpublish_res = await client.post(
            f"/api/v1/marketplace/listings/{listing.listing_id}/unpublish"
        )
        assert unpublish_res.status_code == 200, unpublish_res.text
        assert unpublish_res.json()["status"] == "tombstoned"
        hidden_after_unpublish = await client.get("/api/v1/marketplace/listings")
        assert all(item["listing_id"] != str(listing.listing_id) for item in hidden_after_unpublish.json())

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unauthorized_publisher_upload_blocked(db_session, fake_storage):
    author = Account(account_id=uuid.uuid4(), email=f"alice_{uuid.uuid4().hex[:8]}@talos.dev", role="user")
    attacker = Account(account_id=uuid.uuid4(), email=f"mallory_{uuid.uuid4().hex[:8]}@talos.dev", role="user")
    db_session.add_all([author, attacker])
    await db_session.flush()

    listing = MarketplaceListing(
        listing_id=uuid.uuid4(),
        author_account_id=author.account_id,
        publisher_slug="alice",
        author_username="alice",
        kind="skill",
        slug="alice-skill",
        display_name="Alice Skill",
        status="approved",
        version="1.0.0",
    )
    db_session.add(listing)
    await db_session.commit()

    # Logged in as attacker
    app.dependency_overrides[get_db] = lambda: db_session
    from app.api.v1.marketplace import require_account, get_optional_account
    app.dependency_overrides[require_account] = lambda: attacker
    app.dependency_overrides[get_optional_account] = lambda: attacker

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/marketplace/uploads/init",
            json={
                "listing_id": str(listing.listing_id),
                "version": "1.1.0",
            },
        )
        assert res.status_code == 400
        assert "not authorized" in res.json()["detail"].lower()

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_cloud_proxy_upload_streams_to_storage_and_promotes(db_session, fake_storage):
    author = Account(account_id=uuid.uuid4(), email=f"proxy_{uuid.uuid4().hex[:6]}@example.com")
    db_session.add(author)
    await db_session.flush()
    listing = MarketplaceListing(
        listing_id=uuid.uuid4(),
        author_account_id=author.account_id,
        publisher_slug="proxy-author",
        author_username=author.email.split("@")[0],
        kind="skill",
        slug="proxy-skill",
        display_name="Proxy Skill",
        status="pending_review",
        version="1.0.0",
    )
    db_session.add(listing)
    await db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    from app.api.v1.marketplace import require_account
    app.dependency_overrides[require_account] = lambda: author

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        upload_res = await client.post(
            "/api/v1/marketplace/uploads/file",
            data={
                "listing_id": str(listing.listing_id),
                "version": "1.0.1",
                "async_verification": "false",
            },
            files={"file": ("proxy-skill.zip", _make_valid_skill_zip(), "application/zip")},
        )

    assert upload_res.status_code == 200, upload_res.text
    upload_data = upload_res.json()
    assert upload_data["status"] == "promoted"
    assert await fake_storage.exists(upload_data["canonical_key"])
    assert any(key.startswith("packages/skills/") for key in fake_storage._store)
    app.dependency_overrides.clear()
