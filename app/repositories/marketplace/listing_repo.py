"""
Talos Cloud — Marketplace Listing Repository.
"""

from __future__ import annotations

import uuid
from typing import List, Optional, Tuple
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.marketplace import MarketplaceListing, MarketplacePackageVersion


class ListingRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, listing_id: uuid.UUID) -> Optional[MarketplaceListing]:
        stmt = (
            select(MarketplaceListing)
            .where(MarketplaceListing.listing_id == listing_id)
            .options(selectinload(MarketplaceListing.package_versions))
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_slug(
        self, publisher_slug: str, slug: str, kind: Optional[str] = None
    ) -> Optional[MarketplaceListing]:
        conditions = [
            func.lower(MarketplaceListing.publisher_slug) == publisher_slug.lower(),
            func.lower(MarketplaceListing.slug) == slug.lower(),
        ]
        if kind:
            conditions.append(MarketplaceListing.kind == kind.lower())

        stmt = (
            select(MarketplaceListing)
            .where(*conditions)
            .options(selectinload(MarketplaceListing.package_versions))
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

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
        stmt = select(MarketplaceListing)

        if status and status != "all":
            stmt = stmt.where(MarketplaceListing.status == status)

        if kind and kind != "all":
            stmt = stmt.where(MarketplaceListing.kind == kind.lower())

        if publisher:
            stmt = stmt.where(func.lower(MarketplaceListing.publisher_slug) == publisher.lower())

        if tag:
            # Check if tag is inside tags array or json
            stmt = stmt.where(MarketplaceListing.tags.contains([tag]))

        if q and q.strip():
            term = f"%{q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(MarketplaceListing.display_name).like(term),
                    func.lower(MarketplaceListing.slug).like(term),
                    func.lower(MarketplaceListing.tagline).like(term),
                    func.lower(MarketplaceListing.description).like(term),
                )
            )

        # Count total
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_res = await self.db.execute(count_stmt)
        total = total_res.scalar_one() or 0

        # Paginate and order by install_count desc, created_at desc
        stmt = (
            stmt.order_by(
                MarketplaceListing.install_count.desc(),
                MarketplaceListing.created_at.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        res = await self.db.execute(stmt)
        return list(res.scalars().all()), total

    async def create(self, listing: MarketplaceListing) -> MarketplaceListing:
        self.db.add(listing)
        await self.db.flush()
        return listing

    async def update_install_count(self, listing_id: uuid.UUID, delta: int) -> None:
        new_count = MarketplaceListing.install_count + delta
        stmt = (
            update(MarketplaceListing)
            .where(MarketplaceListing.listing_id == listing_id)
            .values(install_count=case((new_count < 0, 0), else_=new_count))
        )
        await self.db.execute(stmt)
