"""
Talos Cloud — Marketplace Moderation & Admin Audit Service.
"""

from __future__ import annotations

import uuid
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.marketplace.errors import ListingNotFoundError
from app.models.accounts import Account
from app.models.marketplace import MarketplaceAdminAudit
from app.repositories.marketplace.listing_repo import ListingRepository


class ModerationService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.listing_repo = ListingRepository(db)

    async def _audit(self, admin_account: Account, listing_id: uuid.UUID, action: str, reason: Optional[str]):
        audit = MarketplaceAdminAudit(
            admin_account_id=admin_account.account_id,
            listing_id=listing_id,
            action=action,
            reason=reason,
        )
        self.db.add(audit)
        await self.db.flush()

    async def approve(self, admin_account: Account, listing_id: uuid.UUID, reason: Optional[str] = None):
        listing = await self.listing_repo.get_by_id(listing_id)
        if not listing:
            raise ListingNotFoundError(f"Listing '{listing_id}' not found.")
        listing.status = "approved"
        await self._audit(admin_account, listing_id, action="approve", reason=reason)
        await self.db.flush()
        return listing

    async def reject(self, admin_account: Account, listing_id: uuid.UUID, reason: Optional[str] = None):
        listing = await self.listing_repo.get_by_id(listing_id)
        if not listing:
            raise ListingNotFoundError(f"Listing '{listing_id}' not found.")
        listing.status = "rejected"
        await self._audit(admin_account, listing_id, action="reject", reason=reason)
        await self.db.flush()
        return listing

    async def tombstone(self, admin_account: Account, listing_id: uuid.UUID, reason: Optional[str] = None):
        listing = await self.listing_repo.get_by_id(listing_id)
        if not listing:
            raise ListingNotFoundError(f"Listing '{listing_id}' not found.")
        listing.status = "tombstoned"
        await self._audit(admin_account, listing_id, action="tombstone", reason=reason)
        await self.db.flush()
        return listing
