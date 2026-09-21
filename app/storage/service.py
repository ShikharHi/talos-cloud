"""
Talos Cloud — Storage Service.

High-level orchestrator for object storage operations. Decouples business logic
(Marketplace, Validation, Packaging) from provider SDKs (boto3).
"""

import logging
import tempfile
from typing import BinaryIO, Optional

from app.config import Settings, get_settings
from app.storage.base import StorageProvider
from app.storage.exceptions import (
    StorageConfigurationError,
    StorageError,
    StorageNotFoundError,
    StorageValidationError,
)
from app.storage.keys import (
    asset_object_key,
    package_object_key,
    temp_asset_upload_object_key,
    temp_upload_object_key,
)
from app.storage.models import (
    PresignedDownloadUrl,
    PresignedUploadUrl,
    StorageMetadata,
    UploadVerificationResult,
)
from app.storage.package_security import verify_package_stream
from app.storage.s3 import S3StorageProvider

logger = logging.getLogger("talos.storage.service")


import asyncio

class StorageService:
    """
    Central storage service used across Talos Cloud.
    Manages presigned URLs, streaming verification, and object promotion.
    """

    def __init__(self, provider: StorageProvider, default_bucket: str) -> None:
        self.provider = provider
        self.default_bucket = default_bucket
        self._upload_semaphore = asyncio.Semaphore(10)

    # -------------------------------------------------------------------------
    # Core Primitive Operations
    # -------------------------------------------------------------------------

    async def exists(self, key: str) -> bool:
        """Check if an object exists."""
        return await self.provider.exists(key, bucket=self.default_bucket)

    async def get_metadata(self, key: str) -> StorageMetadata:
        """Retrieve object metadata."""
        return await self.provider.get_metadata(key, bucket=self.default_bucket)

    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> StorageMetadata:
        """Upload raw bytes directly to storage."""
        return await self.provider.upload(key=key, data=data, content_type=content_type, bucket=self.default_bucket)

    async def upload_stream(
        self,
        key: str,
        source_file: BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> StorageMetadata:
        """Upload from an open binary file/stream directly to storage without full in-memory buffering."""
        return await self.provider.upload_stream(key=key, source_file=source_file, content_type=content_type, bucket=self.default_bucket)

    async def download(self, key: str) -> bytes:
        """Download complete object contents as bytes."""
        return await self.provider.download(key=key, bucket=self.default_bucket)

    async def download_stream_to_file(
        self,
        key: str,
        target_file: BinaryIO,
        chunk_size: int = 65536,
    ) -> int:
        """Stream object content from storage into an open binary file-like object."""
        return await self.provider.download_stream_to_file(key=key, target_file=target_file, bucket=self.default_bucket, chunk_size=chunk_size)

    async def delete(self, key: str) -> bool:
        """Delete an object from storage."""
        return await self.provider.delete(key, bucket=self.default_bucket)

    # -------------------------------------------------------------------------
    # Presigned Package Upload & Download
    # -------------------------------------------------------------------------

    async def create_package_upload_url(
        self,
        resource_type: str,
        resource_id: str,
        upload_id: str,
        expires_in: int = 900,
        content_type: str = "application/zip",
    ) -> PresignedUploadUrl:
        """
        Generate short-lived presigned PUT URL for package archive upload.
        Uploads always land at a staging key: uploads/{type}/{id}/{upload_id}/package.zip
        """
        temp_key = temp_upload_object_key(resource_type, resource_id, upload_id)
        url = await self.provider.create_upload_url(
            key=temp_key,
            expires_in=expires_in,
            content_type=content_type,
            bucket=self.default_bucket,
        )
        return PresignedUploadUrl(
            upload_id=upload_id,
            object_key=temp_key,
            upload_url=url,
            expires_in=expires_in,
            http_method="PUT",
        )

    async def create_package_download_url(
        self,
        resource_type: str,
        resource_id: str,
        version: str,
        expires_in: int = 900,
        filename: Optional[str] = None,
    ) -> PresignedDownloadUrl:
        """
        Generate short-lived presigned GET URL for downloading an approved package version.
        Target key: {type}/{id}/{version}/package.zip
        """
        canonical_key = package_object_key(resource_type, resource_id, version)

        # Ensure object exists before issuing download URL
        if not await self.exists(canonical_key):
            raise StorageNotFoundError(
                f"Package file not found for {resource_type} '{resource_id}' version '{version}'"
            )

        dl_filename = filename or f"{resource_id}-{version}.zip"
        url = await self.provider.create_download_url(
            key=canonical_key,
            expires_in=expires_in,
            filename=dl_filename,
            bucket=self.default_bucket,
        )
        return PresignedDownloadUrl(download_url=url, expires_in=expires_in)

    # -------------------------------------------------------------------------
    # Presigned Asset Upload & Download
    # -------------------------------------------------------------------------

    async def create_asset_upload_url(
        self,
        resource_type: str,
        resource_id: str,
        upload_id: str,
        filename: str = "icon.png",
        expires_in: int = 900,
        content_type: str = "image/png",
    ) -> PresignedUploadUrl:
        """Generate presigned PUT URL for asset staging."""
        temp_key = temp_asset_upload_object_key(resource_type, resource_id, upload_id, filename)
        url = await self.provider.create_upload_url(
            key=temp_key,
            expires_in=expires_in,
            content_type=content_type,
            bucket=self.default_bucket,
        )
        return PresignedUploadUrl(
            upload_id=upload_id,
            object_key=temp_key,
            upload_url=url,
            expires_in=expires_in,
            http_method="PUT",
        )

    async def create_asset_download_url(
        self,
        resource_type: str,
        resource_id: str,
        filename: str = "icon.png",
        expires_in: int = 900,
    ) -> PresignedDownloadUrl:
        """Generate presigned GET URL for downloading public asset."""
        canonical_key = asset_object_key(resource_type, resource_id, filename)
        if not await self.exists(canonical_key):
            raise StorageNotFoundError(f"Asset '{filename}' not found for {resource_type} '{resource_id}'")

        url = await self.provider.create_download_url(
            key=canonical_key,
            expires_in=expires_in,
            filename=filename,
            bucket=self.default_bucket,
        )
        return PresignedDownloadUrl(download_url=url, expires_in=expires_in)

    # -------------------------------------------------------------------------
    # Verification & Promotion
    # -------------------------------------------------------------------------

    async def verify_and_promote_package(
        self,
        temp_key: str,
        canonical_key: str,
        resource_type: str,
        expected_sha256: Optional[str] = None,
        max_size: int = 52_428_800,
    ) -> UploadVerificationResult:
        """
        Streams uploaded object from temporary key into a spool file,
        runs security/manifest verification, and if valid, copies it to canonical_key
        and removes temp_key.
        """
        from app.storage.package_security import check_disk_space_available
        if not check_disk_space_available(min_free_bytes=200_000_000):
            raise StorageError("Host storage exhausted: insufficient disk space to stage package")

        async with self._upload_semaphore:
            # Stream directly to NamedTemporaryFile to avoid loading entire archive in RAM
            with tempfile.NamedTemporaryFile(mode="w+b", delete=True) as temp_file:
                bytes_streamed = await self.provider.download_stream_to_file(
                    key=temp_key,
                    target_file=temp_file,
                    bucket=self.default_bucket,
                )

                # Perform archive and security inspection
                verification = verify_package_stream(
                    stream_file=temp_file,
                    resource_type=resource_type,
                    expected_sha256=expected_sha256,
                    max_size=max_size,
                )

            if not verification.valid:
                logger.warning(
                    "Package verification failed for '%s': %s",
                    temp_key,
                    verification.errors,
                )
                return verification

            # Promotion: Copy from temporary staging key to canonical immutable key
            logger.info("Promoting verified package: '%s' -> '%s'", temp_key, canonical_key)
            await self.provider.copy(
                source_key=temp_key,
                dest_key=canonical_key,
                bucket=self.default_bucket,
            )

            # Cleanup staging object
            try:
                await self.provider.delete(temp_key, bucket=self.default_bucket)
            except Exception as e:
                logger.warning("Could not delete staging object '%s' after promotion: %s", temp_key, e)

            return verification

    async def promote_asset(
        self,
        temp_key: str,
        canonical_key: str,
    ) -> None:
        """Promotes an uploaded asset from staging key to canonical key."""
        if not await self.exists(temp_key):
            raise StorageNotFoundError(f"Staged asset not found at '{temp_key}'")

        await self.provider.copy(
            source_key=temp_key,
            dest_key=canonical_key,
            bucket=self.default_bucket,
        )
        try:
            await self.provider.delete(temp_key, bucket=self.default_bucket)
        except Exception as e:
            logger.warning("Could not delete staging asset '%s': %s", temp_key, e)


# Global singleton storage service
_storage_service_instance: Optional[StorageService] = None


def get_storage_service(settings: Optional[Settings] = None) -> StorageService:
    """
    Factory / dependency provider for StorageService.
    Initializes S3StorageProvider from application settings.
    """
    global _storage_service_instance
    if _storage_service_instance is not None:
        return _storage_service_instance

    cfg = settings or get_settings()
    if not cfg.is_storage_configured:
        raise StorageConfigurationError(
            "Tigris object storage is not configured. Ensure TIGRIS_ACCESS_KEY_ID, "
            "TIGRIS_SECRET_ACCESS_KEY, TIGRIS_ENDPOINT_URL, and TIGRIS_BUCKET_NAME are set."
        )

    provider = S3StorageProvider(
        endpoint_url=cfg.tigris_endpoint_url,
        access_key_id=cfg.tigris_access_key_id,
        secret_access_key=cfg.tigris_secret_access_key,
        bucket_name=cfg.tigris_bucket_name,
        region_name=cfg.tigris_region,
        addressing_style="path",
    )

    _storage_service_instance = StorageService(
        provider=provider,
        default_bucket=cfg.tigris_bucket_name,
    )
    return _storage_service_instance


def reset_storage_service() -> None:
    """Resets cached StorageService singleton (useful in test teardown/setup)."""
    global _storage_service_instance
    _storage_service_instance = None
