"""
Talos Cloud — Marketplace Listing Service.
"""

from __future__ import annotations

import uuid
from typing import List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.marketplace.errors import (
    ListingNotFoundError,
    ListingSlugConflictError,
    PublishNotAllowedError,
)
from app.models.accounts import Account
from app.models.marketplace import MarketplaceListing
from app.repositories.marketplace.listing_repo import ListingRepository


class ListingService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ListingRepository(db)

    async def search(
        self,
        q: Optional[str] = None,
        kind: Optional[str] = None,
        tag: Optional[str] = None,
        publisher: Optional[str] = None,
        status: Optional[str] = "approved",
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[MarketplaceListing], int]:
        return await self.repo.search(
            q=q, kind=kind, tag=tag, publisher=publisher, status=status, page=page, page_size=page_size
        )

    async def get_by_id(self, listing_id: uuid.UUID) -> MarketplaceListing:
        listing = await self.repo.get_by_id(listing_id)
        if not listing:
            raise ListingNotFoundError(f"Marketplace listing '{listing_id}' not found.")
        return listing

    async def get_by_slug(self, publisher_slug: str, slug: str, kind: Optional[str] = None) -> MarketplaceListing:
        listing = await self.repo.get_by_slug(publisher_slug, slug, kind=kind)
        if not listing:
            raise ListingNotFoundError(f"Listing '{publisher_slug}/{slug}' not found.")
        return listing

    async def create_listing(
        self,
        account: Account,
        publisher_slug: str,
        slug: str,
        kind: str,
        display_name: str,
        tagline: str = "",
        description: str = "",
        icon_emoji: str = "📦",
        icon_color: str = "#a3e635",
        tags: Optional[List[str]] = None,
        manifest_yaml: str = "",
        visibility: str = "public",
    ) -> MarketplaceListing:
        k = kind.strip().lower()
        # Check slug conflict
        existing = await self.repo.get_by_slug(publisher_slug, slug, kind=k)
        if existing:
            raise ListingSlugConflictError(
                f"Listing with slug '{slug}' already exists under publisher '{publisher_slug}'."
            )

        author_name = account.email.split("@")[0] if account.email else publisher_slug

        listing = MarketplaceListing(
            author_account_id=account.account_id,
            publisher_slug=publisher_slug,
            author_username=author_name,
            kind=k,
            slug=slug,
            display_name=display_name,
            tagline=tagline,
            description=description,
            icon_emoji=icon_emoji,
            icon_color=icon_color,
            tags=tags or [],
            manifest_yaml=manifest_yaml,
            visibility=visibility,
            status="approved",  # Initial policy can be approved or pending_review
        )
        return await self.repo.create(listing)

    async def unpublish(self, account: Account, listing_id: uuid.UUID) -> MarketplaceListing:
        listing = await self.get_by_id(listing_id)
        if listing.author_account_id != account.account_id:
            raise PublishNotAllowedError("You do not have permission to unpublish this listing.")

        listing.status = "tombstoned"
        await self.db.flush()
        return listing
