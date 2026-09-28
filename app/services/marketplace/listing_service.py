"""
Talos Cloud — Marketplace Listing Service.
"""

from __future__ import annotations

import uuid
from typing import List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.marketplace.errors import (
    ListingNotFoundError,
    ListingSlugConflictError,
    PublishNotAllowedError,
)
from app.models.accounts import Account
from app.models.marketplace import MarketplaceListing, PackageUpload
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
        pricing_type: Optional[str] = None,
        verified: Optional[bool] = None,
        status: Optional[str] = "approved",
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[MarketplaceListing], int]:
        from app.services.marketplace.search_service import WeaviateSearchService
        weaviate_service = WeaviateSearchService()

        # Attempt Weaviate hybrid vector + keyword query if search term is provided
        if q and q.strip() and weaviate_service.is_configured:
            item_ids = await weaviate_service.hybrid_search(
                query=q,
                kind=kind,
                tag=tag,
                pricing_type=pricing_type,
                verified=verified,
                limit=page_size,
            )
            if item_ids:
                # Fetch matching listings from Neon DB (authoritative source of truth)
                try:
                    uuid_list = [uuid.UUID(x) for x in item_ids]
                    stmt = select(MarketplaceListing).where(MarketplaceListing.listing_id.in_(uuid_list))
                    res = await self.db.execute(stmt)
                    items_by_id = {str(item.listing_id): item for item in res.scalars().all()}
                    # Preserve Weaviate hybrid ranking order
                    ranked = [items_by_id[str(uid)] for uid in uuid_list if str(uid) in items_by_id]
                    return ranked, len(ranked)
                except Exception as e:
                    pass

        # Authoritative SQL lexical search fallback
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
        pricing_type: str = "free",
        price_credits: int = 0,
        version_policy: str = "all_minor_patch",
    ) -> MarketplaceListing:
        k = kind.strip().lower()
        # Check slug conflict
        existing = await self.repo.get_by_slug(publisher_slug, slug, kind=k)
        if existing:
            if (
                k == "skill"
                and existing.status == "pending_review"
                and existing.author_account_id == account.account_id
            ):
                active_upload = await self.db.execute(
                    select(PackageUpload.upload_id)
                    .where(
                        PackageUpload.listing_id == existing.listing_id,
                        PackageUpload.status.in_(
                            ["pending", "uploading", "verifying", "verified", "promoting"]
                        ),
                    )
                    .limit(1)
                )
                if active_upload.scalar_one_or_none():
                    raise ListingSlugConflictError(
                        f"Skill '{slug}' already has an upload being processed."
                    )

                existing.display_name = display_name
                existing.tagline = tagline
                existing.description = description
                existing.icon_emoji = icon_emoji
                existing.icon_color = icon_color
                existing.tags = tags or []
                existing.manifest_yaml = manifest_yaml
                existing.visibility = visibility
                existing.pricing_type = pricing_type
                existing.price_credits = price_credits
                existing.version_policy = version_policy
                await self.db.flush()
                return existing
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
            pricing_type=pricing_type,
            price_credits=price_credits,
            version_policy=version_policy,
            status="pending_review" if k == "skill" else "approved",
        )
        return await self.repo.create(listing)

    async def unpublish(self, account: Account, listing_id: uuid.UUID) -> MarketplaceListing:
        listing = await self.get_by_id(listing_id)
        if listing.author_account_id != account.account_id and getattr(account, "role", "user") != "admin":
            raise PublishNotAllowedError("You do not have permission to unpublish this listing.")

        listing.status = "tombstoned"
        await self.db.flush()

        # Synchronize removal with Weaviate search index
        try:
            from app.services.marketplace.search_service import WeaviateSearchService
            weaviate_svc = WeaviateSearchService()
            if weaviate_svc.is_configured:
                await weaviate_svc.delete_item(str(listing_id))
        except Exception as e:
            pass

        return listing
