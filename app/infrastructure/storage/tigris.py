"""
Talos Cloud — Tigris S3 Storage Client for Marketplace.

Encapsulates object key generation, presigned URL creation, streaming verification,
and atomic/idempotent promotion to canonical release keys.
"""

from __future__ import annotations

import logging
import uuid
from typing import BinaryIO, Optional

from app.domain.marketplace.errors import VersionImmutableError
from app.storage.service import get_storage_service

logger = logging.getLogger("talos.marketplace.storage")


def canonical_package_key(kind: str, listing_id: uuid.UUID | str, version: str) -> str:
    """Generates immutable canonical key: packages/{kind}/{listing_id}/{version}/package.zip"""
    k = kind.strip().lower()
    plural_kind = "skills" if k in ("skill", "skills") else ("agents" if k in ("agent", "agents") else ("mcp" if k == "mcp" else "tools"))
    return f"packages/{plural_kind}/{listing_id}/{version}/package.zip"


def staging_package_key(kind: str, listing_id: uuid.UUID | str, upload_id: uuid.UUID | str) -> str:
    """Generates temporary staging key: uploads/{kind}/{listing_id}/{upload_id}/package.zip"""
    k = kind.strip().lower()
    plural_kind = "skills" if k in ("skill", "skills") else ("agents" if k in ("agent", "agents") else ("mcp" if k == "mcp" else "tools"))
    return f"uploads/{plural_kind}/{listing_id}/{upload_id}/package.zip"


class TigrisMarketplaceStorage:
    """
    Marketplace-specific storage orchestrator using Tigris S3.
    """

    def __init__(self, storage_service=None):
        self._storage = storage_service or get_storage_service()

    async def generate_presigned_upload_url(
        self,
        staging_key: str,
        expires_in: int = 900,
        content_type: str = "application/zip",
    ) -> str:
        """Generates a short-lived presigned PUT URL for an exact staging key."""
        return await self._storage.provider.create_upload_url(
            key=staging_key,
            expires_in=expires_in,
            content_type=content_type,
            bucket=self._storage.default_bucket,
        )

    async def generate_presigned_download_url(
        self,
        canonical_key: str,
        expires_in: int = 900,
        filename: Optional[str] = None,
    ) -> str:
        """Generates a short-lived presigned GET URL for an exact canonical key."""
        return await self._storage.provider.create_download_url(
            key=canonical_key,
            expires_in=expires_in,
            bucket=self._storage.default_bucket,
            filename=filename,
        )

    async def exists(self, key: str) -> bool:
        return await self._storage.exists(key)

    async def download_to_file(self, key: str, target_file: BinaryIO) -> int:
        """Streams an object from storage to a local file pointer."""
        return await self._storage.download_stream_to_file(key, target_file)

    async def promote_staging_to_canonical(
        self,
        staging_key: str,
        canonical_key: str,
        expected_sha256: str,
    ) -> None:
        """
        Idempotently promotes a staging object to its canonical release key.
        If canonical key already exists:
          - If sha256 matches: idempotent success
          - If sha256 differs: raises VersionImmutableError (hard conflict)
        """
        if await self._storage.exists(canonical_key):
            meta = await self._storage.get_metadata(canonical_key)
            existing_hash = meta.sha256
            if existing_hash and existing_hash.lower() != expected_sha256.lower():
                raise VersionImmutableError(
                    f"Canonical key '{canonical_key}' already exists with a different SHA-256 checksum! Overwrites are forbidden."
                )
            logger.info("Canonical key %s already exists with matching checksum (idempotent promotion)", canonical_key)
            return

        # Perform server-side or stream copy
        data = await self._storage.download(staging_key)
        await self._storage.upload(
            key=canonical_key,
            data=data,
            content_type="application/zip",
        )
        logger.info("Successfully promoted %s -> %s", staging_key, canonical_key)

    async def delete(self, key: str) -> bool:
        return await self._storage.delete(key)
