"""
Unit tests for StorageProvider and S3StorageProvider.
"""

import io
from datetime import datetime, timezone
import pytest
from botocore.exceptions import ClientError

from app.storage.base import StorageProvider
from app.storage.exceptions import (
    StorageDownloadError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageUploadError,
)
from app.storage.models import StorageMetadata
from app.storage.s3 import S3StorageProvider


class FakeStorageProvider(StorageProvider):
    """In-memory StorageProvider fake for fast unit testing."""

    def __init__(self, default_bucket: str = "talos-marketplace") -> None:
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

    async def delete(self, key: str, bucket: str | None = None) -> bool:
        if key in self._store:
            del self._store[key]
            return True
        return False

    async def create_upload_url(
        self,
        key: str,
        expires_in: int = 900,
        content_type: str | None = None,
        bucket: str | None = None,
    ) -> str:
        return f"https://t3.storage.dev/{bucket or self.default_bucket}/{key}?signed=put"

    async def create_download_url(
        self,
        key: str,
        expires_in: int = 900,
        filename: str | None = None,
        bucket: str | None = None,
    ) -> str:
        return f"https://t3.storage.dev/{bucket or self.default_bucket}/{key}?signed=get"

    async def copy(self, source_key: str, dest_key: str, bucket: str | None = None) -> None:
        if source_key not in self._store:
            raise StorageNotFoundError(f"Source object '{source_key}' not found")
        self._store[dest_key] = self._store[source_key]

    async def download_stream_to_file(
        self,
        key: str,
        target_file: io.BytesIO,
        bucket: str | None = None,
        chunk_size: int = 65536,
    ) -> int:
        if key not in self._store:
            raise StorageNotFoundError(f"Object '{key}' not found")
        data = self._store[key]
        target_file.write(data)
        return len(data)


@pytest.mark.asyncio
async def test_fake_provider_crud():
    """Verifies standard storage provider contracts."""
    provider = FakeStorageProvider()
    key = "agents/coder/1.0.0/package.zip"

    assert await provider.exists(key) is False

    # Upload
    meta = await provider.upload(key, b"dummy-zip-data", "application/zip")
    assert meta.size == 14
    assert await provider.exists(key) is True

    # Download
    data = await provider.download(key)
    assert data == b"dummy-zip-data"

    # Copy
    copy_key = "agents/coder/1.0.1/package.zip"
    await provider.copy(key, copy_key)
    assert await provider.exists(copy_key) is True

    # Delete
    assert await provider.delete(key) is True
    assert await provider.exists(key) is False


def test_s3_error_mapping():
    """Tests that S3 ClientError instances are properly mapped without credential leaks."""
    provider = S3StorageProvider(
        endpoint_url="https://t3.storage.dev",
        access_key_id="dummy",
        secret_access_key="dummy",
        bucket_name="talos-marketplace",
    )

    not_found_err = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
        "GetObject",
    )
    with pytest.raises(StorageNotFoundError):
        provider._handle_client_error(not_found_err, "download", "test.zip")

    forbidden_err = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
        "PutObject",
    )
    with pytest.raises(StoragePermissionError):
        provider._handle_client_error(forbidden_err, "upload", "test.zip")
