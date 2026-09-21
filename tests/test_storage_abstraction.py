"""
Unit and Integration Tests for Object Storage Layer & Abstraction.

Verifies:
  - Canonical key generation conforming strictly to single-bucket prefix hierarchy:
    agents/, skills/, mcp/, assets/, uploads/
  - Core primitives: upload, upload_stream, download, download_stream_to_file, exists, delete
  - Presigned upload & download URL generation with TTL
  - Streamed package verification & promotion with SHA-256 validation
  - Integrity failure handling (SHA-256 mismatch prevents promotion)
  - Abandoned / expired upload lifecycle cleanup in storage and database
"""

import hashlib
import io
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from app.models.accounts import Account
from app.models.marketplace import MarketplaceListing, PackageUpload
from app.services.storage_cleanup import cleanup_abandoned_uploads
from app.storage.base import StorageProvider
from app.storage.exceptions import StorageNotFoundError
from app.storage.keys import (
    asset_object_key,
    package_object_key,
    temp_asset_upload_object_key,
    temp_upload_object_key,
)
from app.storage.models import StorageMetadata, UploadState
from app.storage.service import StorageService


class InMemoryStorageProvider(StorageProvider):
    """Full in-memory implementation of StorageProvider for testing streaming and primitives."""

    def __init__(self, default_bucket: str = "talos-marketplace"):
        self.default_bucket = default_bucket
        self._store: dict[str, bytes] = {}

    async def exists(self, key: str, bucket: str | None = None) -> bool:
        return key in self._store

    async def get_metadata(self, key: str, bucket: str | None = None) -> StorageMetadata:
        if key not in self._store:
            raise StorageNotFoundError(f"Object '{key}' not found")
        data = self._store[key]
        return StorageMetadata(
            key=key,
            bucket=bucket or self.default_bucket,
            size=len(data),
            content_type="application/zip",
            last_modified=datetime.now(timezone.utc),
        )

    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        bucket: str | None = None,
    ) -> StorageMetadata:
        self._store[key] = data
        return StorageMetadata(
            key=key,
            bucket=bucket or self.default_bucket,
            size=len(data),
            content_type=content_type,
            last_modified=datetime.now(timezone.utc),
        )

    async def upload_stream(
        self,
        key: str,
        source_file,
        content_type: str = "application/octet-stream",
        bucket: str | None = None,
    ) -> StorageMetadata:
        data = source_file.read()
        self._store[key] = data
        return StorageMetadata(
            key=key,
            bucket=bucket or self.default_bucket,
            size=len(data),
            content_type=content_type,
            last_modified=datetime.now(timezone.utc),
        )

    async def download(self, key: str, bucket: str | None = None) -> bytes:
        if key not in self._store:
            raise StorageNotFoundError(f"Object '{key}' not found")
        return self._store[key]

    async def download_stream_to_file(
        self,
        key: str,
        target_file,
        bucket: str | None = None,
        chunk_size: int = 65536,
    ) -> int:
        if key not in self._store:
            raise StorageNotFoundError(f"Object '{key}' not found")
        data = self._store[key]
        target_file.write(data)
        target_file.seek(0)
        return len(data)

    async def delete(self, key: str, bucket: str | None = None) -> bool:
        if key in self._store:
            del self._store[key]
            return True
        return False

    async def copy(
        self,
        source_key: str,
        dest_key: str,
        bucket: str | None = None,
    ) -> None:
        if source_key not in self._store:
            raise StorageNotFoundError(f"Source object '{source_key}' not found")
        data = self._store[source_key]
        self._store[dest_key] = data

    async def create_upload_url(
        self,
        key: str,
        expires_in: int = 900,
        content_type: str | None = None,
        bucket: str | None = None,
    ) -> str:
        return f"https://s3.tigris.dev/{bucket or self.default_bucket}/{key}?upload=true&exp={expires_in}"

    async def create_download_url(
        self,
        key: str,
        expires_in: int = 900,
        filename: str | None = None,
        bucket: str | None = None,
    ) -> str:
        fn_param = f"&filename={filename}" if filename else ""
        return f"https://s3.tigris.dev/{bucket or self.default_bucket}/{key}?exp={expires_in}{fn_param}"


def _create_valid_package_zip(resource_type: str, manifest_name: str) -> bytes:
    """Creates a valid zip archive in memory with proper manifest for the resource type."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if resource_type in ("agent", "agents"):
            zf.writestr(
                "agent.yaml",
                "name: " + manifest_name + "\nversion: 1.0.0\ndescription: Test Agent\n",
            )
            zf.writestr("main.py", "# agent entrypoint\nprint('agent ready')\n")
        elif resource_type in ("skill", "skills"):
            zf.writestr(
                "SKILL.md",
                "---\nname: " + manifest_name + "\ndescription: Test Skill\n---\n# Skill Documentation\n",
            )
        elif resource_type in ("mcp", "tools"):
            zf.writestr(
                "mcp.yaml",
                "name: " + manifest_name + "\nversion: 1.0.0\n",
            )
    return buf.getvalue()


# =============================================================================
# TESTS
# =============================================================================

def test_storage_prefix_hierarchy_conventions():
    """Verifies that all storage keys conform strictly to single-bucket prefix rules."""
    assert package_object_key("agents", "finance-advisor", "1.0.0") == "agents/finance-advisor/1.0.0/package.zip"
    assert package_object_key("skills", "web-scraper", "2.1.0") == "skills/web-scraper/2.1.0/package.zip"
    assert package_object_key("mcp", "postgres-mcp", "0.5.0") == "mcp/postgres-mcp/0.5.0/package.zip"

    assert asset_object_key("agents", "finance-advisor", "icon.png") == "assets/agents/finance-advisor/icon.png"
    assert asset_object_key("skills", "web-scraper", "preview.jpg") == "assets/skills/web-scraper/preview.jpg"

    assert temp_upload_object_key("agents", "finance-advisor", "up-12345") == "uploads/agents/finance-advisor/up-12345/package.zip"
    assert temp_asset_upload_object_key("skills", "web-scraper", "up-999", "logo.png") == "uploads/assets/skills/web-scraper/up-999/logo.png"


@pytest.mark.asyncio
async def test_storage_service_crud_primitives():
    """Tests basic CRUD primitives: upload, exists, get_metadata, download, delete."""
    provider = InMemoryStorageProvider(default_bucket="talos-marketplace")
    service = StorageService(provider=provider, default_bucket="talos-marketplace")

    key = "skills/summarizer/1.0.0/package.zip"
    test_data = b"SAMPLE_PACKAGE_ZIP_DATA"

    # 1. Upload
    meta = await service.upload(key=key, data=test_data, content_type="application/zip")
    assert meta.key == key
    assert meta.size == len(test_data)
    assert meta.bucket == "talos-marketplace"

    # 2. Exists
    assert await service.exists(key) is True
    assert await service.exists("nonexistent/key") is False

    # 3. Metadata
    fetched_meta = await service.get_metadata(key)
    assert fetched_meta.size == len(test_data)

    # 4. Download
    downloaded = await service.download(key)
    assert downloaded == test_data

    # 5. Delete
    deleted = await service.delete(key)
    assert deleted is True
    assert await service.exists(key) is False


@pytest.mark.asyncio
async def test_storage_service_streaming_upload_and_download():
    """Tests streaming primitives (upload_stream and download_stream_to_file)."""
    provider = InMemoryStorageProvider(default_bucket="talos-marketplace")
    service = StorageService(provider=provider, default_bucket="talos-marketplace")

    key = "agents/coder/2.0.0/package.zip"
    payload = b"STREAMING_BINARY_PAYLOAD" * 1024  # ~24 KB

    # 1. upload_stream
    stream_in = io.BytesIO(payload)
    meta = await service.upload_stream(key=key, source_file=stream_in, content_type="application/zip")
    assert meta.size == len(payload)

    # 2. download_stream_to_file
    stream_out = io.BytesIO()
    bytes_read = await service.download_stream_to_file(key=key, target_file=stream_out)
    assert bytes_read == len(payload)
    assert stream_out.getvalue() == payload


@pytest.mark.asyncio
async def test_presigned_urls_generation():
    """Tests generation of presigned PUT upload and GET download URLs with TTL."""
    provider = InMemoryStorageProvider(default_bucket="talos-marketplace")
    service = StorageService(provider=provider, default_bucket="talos-marketplace")

    # Upload URL
    up_res = await service.create_package_upload_url(
        resource_type="skills",
        resource_id="summarizer",
        upload_id="up-42",
        expires_in=600,
    )
    assert up_res.upload_id == "up-42"
    assert up_res.object_key == "uploads/skills/summarizer/up-42/package.zip"
    assert "upload=true" in up_res.upload_url
    assert "exp=600" in up_res.upload_url

    # Download URL when object exists
    canonical_key = "skills/summarizer/1.0.0/package.zip"
    await service.upload(canonical_key, b"dummy zip")

    dl_res = await service.create_package_download_url(
        resource_type="skills",
        resource_id="summarizer",
        version="1.0.0",
        expires_in=300,
    )
    assert "exp=300" in dl_res.download_url
    assert "filename=summarizer-1.0.0.zip" in dl_res.download_url

    # Nonexistent package download URL raises StorageNotFoundError
    with pytest.raises(StorageNotFoundError):
        await service.create_package_download_url(
            resource_type="skills",
            resource_id="summarizer",
            version="9.9.9",
        )


@pytest.mark.asyncio
async def test_package_verification_and_promotion_success():
    """
    Tests staged upload streaming verification, SHA-256 validation,
    and atomic promotion to canonical package key.
    """
    provider = InMemoryStorageProvider(default_bucket="talos-marketplace")
    service = StorageService(provider=provider, default_bucket="talos-marketplace")

    zip_bytes = _create_valid_package_zip(resource_type="agents", manifest_name="coder")
    expected_sha = hashlib.sha256(zip_bytes).hexdigest()

    temp_key = temp_upload_object_key("agents", "coder", "up-success-1")
    canonical_key = package_object_key("agents", "coder", "1.0.0")

    # Staging upload
    await service.upload(key=temp_key, data=zip_bytes)
    assert await service.exists(temp_key) is True

    # Verify and promote
    res = await service.verify_and_promote_package(
        temp_key=temp_key,
        canonical_key=canonical_key,
        resource_type="agents",
        expected_sha256=expected_sha,
    )

    assert res.valid is True
    assert res.sha256 == expected_sha
    assert res.file_size == len(zip_bytes)
    assert res.errors == []

    # Canonical key now exists, temp key cleaned up
    assert await service.exists(canonical_key) is True
    assert await service.exists(temp_key) is False


@pytest.mark.asyncio
async def test_package_verification_sha256_mismatch_fails_promotion():
    """
    Tests that a package whose SHA-256 does not match expectations is rejected
    and NEVER promoted to canonical key.
    """
    provider = InMemoryStorageProvider(default_bucket="talos-marketplace")
    service = StorageService(provider=provider, default_bucket="talos-marketplace")

    zip_bytes = _create_valid_package_zip(resource_type="agents", manifest_name="tampered-agent")
    actual_sha = hashlib.sha256(zip_bytes).hexdigest()
    wrong_sha = "0" * 64

    temp_key = temp_upload_object_key("agents", "tampered-agent", "up-fail-sha")
    canonical_key = package_object_key("agents", "tampered-agent", "1.0.0")

    await service.upload(key=temp_key, data=zip_bytes)

    res = await service.verify_and_promote_package(
        temp_key=temp_key,
        canonical_key=canonical_key,
        resource_type="agents",
        expected_sha256=wrong_sha,
    )

    assert res.valid is False
    assert any("Checksum mismatch" in err for err in res.errors)

    # Canonical key MUST NOT exist
    assert await service.exists(canonical_key) is False


@pytest.mark.asyncio
async def test_abandoned_upload_storage_cleanup(db_session):
    """
    Verifies that abandoned / expired staging uploads are cleaned up from storage
    and marked as expired in the database by cleanup_abandoned_uploads.
    """
    provider = InMemoryStorageProvider(default_bucket="talos-marketplace")
    service = StorageService(provider=provider, default_bucket="talos-marketplace")

    author = Account(email="cleanup_author@talos.dev", role="user")
    db_session.add(author)
    await db_session.commit()
    author_id = author.account_id

    listing = MarketplaceListing(
        author_account_id=author_id,
        author_username="cleanup_author",
        kind="agent",
        slug="cleanup-agent",
        display_name="Cleanup Agent",
        version="1.0.0",
        status="pending",
    )
    db_session.add(listing)
    await db_session.commit()
    listing_id = listing.listing_id

    # 1. Staged upload that expired 10 minutes ago
    expired_upload_id = uuid.uuid4()
    expired_key = temp_upload_object_key("agents", "cleanup-agent", str(expired_upload_id))
    await service.upload(expired_key, b"orphaned staging file bytes")

    expired_upload = PackageUpload(
        upload_id=expired_upload_id,
        account_id=author_id,
        listing_id=listing_id,
        resource_type="agent",
        resource_id="cleanup-agent",
        version="1.0.0",
        object_key=expired_key,
        bucket="talos-marketplace",
        status=UploadState.PENDING.value,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )

    # 2. Active upload that expires in 15 minutes
    active_upload_id = uuid.uuid4()
    active_key = temp_upload_object_key("agents", "cleanup-agent", str(active_upload_id))
    await service.upload(active_key, b"fresh staging file bytes")

    active_upload = PackageUpload(
        upload_id=active_upload_id,
        account_id=author_id,
        listing_id=listing_id,
        resource_type="agent",
        resource_id="cleanup-agent",
        version="1.1.0",
        object_key=active_key,
        bucket="talos-marketplace",
        status=UploadState.PENDING.value,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )

    db_session.add_all([expired_upload, active_upload])
    await db_session.commit()

    # Execute cleanup job
    stats = await cleanup_abandoned_uploads(db=db_session, storage=service)

    assert stats["stale_found"] == 1
    assert stats["storage_deleted"] == 1
    assert stats["expired_marked"] == 1

    # Stale file deleted from storage; active file preserved
    assert await service.exists(expired_key) is False
    assert await service.exists(active_key) is True

    # Database state check
    refreshed_expired = (await db_session.execute(select(PackageUpload).where(PackageUpload.upload_id == expired_upload_id))).scalars().first()
    assert refreshed_expired.status == UploadState.EXPIRED.value

    refreshed_active = (await db_session.execute(select(PackageUpload).where(PackageUpload.upload_id == active_upload_id))).scalars().first()
    assert refreshed_active.status == UploadState.PENDING.value
