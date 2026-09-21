"""
Talos Cloud — Marketplace Review Repository.
"""

from __future__ import annotations

import uuid
from typing import List, Optional, Tuple
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketplace import MarketplaceReview


class ReviewRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_listing_and_account(
        self, listing_id: uuid.UUID, account_id: uuid.UUID
    ) -> Optional[MarketplaceReview]:
        stmt = select(MarketplaceReview).where(
            MarketplaceReview.listing_id == listing_id,
            MarketplaceReview.account_id == account_id,
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_listing(
        self, listing_id: uuid.UUID, page: int = 1, page_size: int = 20
    ) -> Tuple[List[MarketplaceReview], int]:
        stmt = select(MarketplaceReview).where(
            MarketplaceReview.listing_id == listing_id,
            MarketplaceReview.status == "published",
        )

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_res = await self.db.execute(count_stmt)
        total = total_res.scalar_one() or 0

        stmt = stmt.order_by(MarketplaceReview.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        res = await self.db.execute(stmt)
        return list(res.scalars().all()), total

    async def upsert_review(
        self, listing_id: uuid.UUID, account_id: uuid.UUID, rating: int, comment: str
    ) -> MarketplaceReview:
        existing = await self.get_by_listing_and_account(listing_id, account_id)
        if existing:
            existing.rating = rating
            existing.comment = comment
            await self.db.flush()
            return existing

        review = MarketplaceReview(
            listing_id=listing_id,
            account_id=account_id,
            rating=rating,
            comment=comment,
        )
        self.db.add(review)
        await self.db.flush()
        return review
