"""
Talos Cloud — Marketplace Installation Service.

Enforces two-phase installation:
  1. init_install: Resolves exact version, issues presigned GET URL and single-use install token.
     Does NOT increment install counter.
  2. complete_install: Only called AFTER local download, local SHA-256 verification,
     and atomic staging succeed. Validates install token, marks active, and updates install count.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.marketplace.errors import (
    InstallNotAuthorizedError,
    InstallTokenExpiredError,
    ListingNotFoundError,
    VersionNotFoundError,
)
from app.domain.marketplace.installation import InstallStatus
from app.infrastructure.storage.tigris import TigrisMarketplaceStorage
from app.models.accounts import Account
from app.models.marketplace import UserInstall
from app.repositories.marketplace.install_repo import InstallRepository
from app.repositories.marketplace.listing_repo import ListingRepository
from app.repositories.marketplace.version_repo import VersionRepository


class InstallService:
    def __init__(self, db: AsyncSession, storage: Optional[TigrisMarketplaceStorage] = None):
        self.db = db
        self.install_repo = InstallRepository(db)
        self.listing_repo = ListingRepository(db)
        self.version_repo = VersionRepository(db)
        self.storage = storage or TigrisMarketplaceStorage()

    async def init_install(
        self,
        account: Account,
        listing_id: Optional[uuid.UUID] = None,
        publisher: Optional[str] = None,
        slug: Optional[str] = None,
        requested_version: Optional[str] = None,
        expires_in: int = 300,
    ) -> dict[str, Any]:
        # 1. Resolve listing
        listing = None
        if listing_id:
            listing = await self.listing_repo.get_by_id(listing_id)
        elif publisher and slug:
            listing = await self.listing_repo.get_by_slug(publisher, slug)

        if not listing:
            raise ListingNotFoundError("Marketplace listing not found.")

        # 2. Resolve exact version (never leave as 'latest')
        version_record = None
        if requested_version and requested_version.lower() != "latest":
            version_record = await self.version_repo.get_by_listing_and_version(
                listing.listing_id, requested_version
            )
        else:
            version_record = await self.version_repo.get_latest_published(listing.listing_id)

        if not version_record:
            raise VersionNotFoundError(
                f"No published version found for listing '{listing.slug}' (requested: {requested_version or 'latest'})."
            )

        # 3. Generate presigned download URL for Tigris canonical release object
        download_url = await self.storage.generate_presigned_download_url(
            canonical_key=version_record.storage_key,
            expires_in=expires_in,
            filename=f"{listing.slug}-{version_record.version}.zip",
        )

        # 4. Generate single-use installation confirmation token
        install_token = f"tok_{secrets.token_urlsafe(32)}"

        # 5. Create or stage user installation in pending_download state
        existing = await self.install_repo.get_by_account_and_listing(
            account.account_id, listing.listing_id
        )
        if not existing:
            install = UserInstall(
                account_id=account.account_id,
                listing_id=listing.listing_id,
                version_id=version_record.version_id,
                installed_version=version_record.version,
                status=InstallStatus.PENDING_DOWNLOAD.value,
                install_token=install_token,
            )
            await self.install_repo.create(install)
        else:
            existing.version_id = version_record.version_id
            existing.installed_version = version_record.version
            existing.status = InstallStatus.PENDING_DOWNLOAD.value
            existing.install_token = install_token
            await self.db.flush()

        return {
            "listing_id": str(listing.listing_id),
            "publisher": listing.publisher_slug,
            "slug": listing.slug,
            "kind": listing.kind,
            "display_name": listing.display_name,
            "version": version_record.version,
            "sha256": version_record.sha256,
            "file_size": version_record.file_size,
            "download_url": download_url,
            "install_token": install_token,
            "expires_in": expires_in,
        }

    async def complete_install(
        self,
        account: Account,
        listing_id: uuid.UUID,
        install_token: str,
    ) -> dict[str, Any]:
        install = await self.install_repo.get_by_token(install_token)
        if not install or install.account_id != account.account_id or install.listing_id != listing_id:
            raise InstallNotAuthorizedError("Invalid or unauthorized install confirmation token.")

        if install.status == InstallStatus.ACTIVE.value:
            # Idempotent success
            return {"ok": True, "status": "active", "version": install.installed_version}

        install.status = InstallStatus.ACTIVE.value
        install.install_token = None
        await self.listing_repo.update_install_count(listing_id, delta=1)
        await self.db.flush()

        return {"ok": True, "status": "active", "version": install.installed_version}

    async def uninstall(
        self,
        account: Account,
        listing_id: Optional[uuid.UUID] = None,
        publisher: Optional[str] = None,
        slug: Optional[str] = None,
    ) -> dict[str, Any]:
        listing = None
        if listing_id:
            listing = await self.listing_repo.get_by_id(listing_id)
        elif publisher and slug:
            listing = await self.listing_repo.get_by_slug(publisher, slug)

        if not listing:
            return {"ok": True, "status": "removed"}

        install = await self.install_repo.get_by_account_and_listing(
            account.account_id, listing.listing_id
        )
        if install and install.status == InstallStatus.ACTIVE.value:
            install.status = InstallStatus.REMOVED.value
            await self.listing_repo.update_install_count(listing.listing_id, delta=-1)
            await self.db.flush()

        return {"ok": True, "status": "removed"}

    async def list_user_installs(self, account: Account) -> List[UserInstall]:
        return await self.install_repo.list_by_account(account.account_id, status="active")
