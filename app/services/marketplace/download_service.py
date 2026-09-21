"""
Talos Cloud — Marketplace Download Service.

INVARIANT: GET /download MUST NOT have side effects!
It must NOT increment install counters, create installations, or mutate database state.
Downloads are purely idempotent read operations.
"""

from __future__ import annotations

import uuid
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.marketplace.errors import ListingNotFoundError, VersionNotFoundError
from app.infrastructure.storage.tigris import TigrisMarketplaceStorage
from app.repositories.marketplace.listing_repo import ListingRepository
from app.repositories.marketplace.version_repo import VersionRepository


class DownloadService:
    def __init__(self, db: AsyncSession, storage: Optional[TigrisMarketplaceStorage] = None):
        self.db = db
        self.listing_repo = ListingRepository(db)
        self.version_repo = VersionRepository(db)
        self.storage = storage or TigrisMarketplaceStorage()

    async def get_download_url(
        self,
        publisher: str,
        slug: str,
        version: Optional[str] = None,
        expires_in: int = 900,
    ) -> str:
        listing = await self.listing_repo.get_by_slug(publisher, slug)
        if not listing:
            raise ListingNotFoundError(f"Listing '{publisher}/{slug}' not found.")

        if version and version.lower() != "latest":
            ver = await self.version_repo.get_by_listing_and_version(listing.listing_id, version)
        else:
            ver = await self.version_repo.get_latest_published(listing.listing_id)

        if not ver:
            raise VersionNotFoundError(f"No published package version found for '{publisher}/{slug}'.")

        return await self.storage.generate_presigned_download_url(
            canonical_key=ver.storage_key,
            expires_in=expires_in,
            filename=f"{slug}-{ver.version}.zip",
        )
