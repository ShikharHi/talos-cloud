"""
Talos Cloud — Marketplace Version Repository.
"""

from __future__ import annotations

import uuid
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketplace import MarketplacePackageVersion


class VersionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, version_id: uuid.UUID) -> Optional[MarketplacePackageVersion]:
        stmt = select(MarketplacePackageVersion).where(
            MarketplacePackageVersion.version_id == version_id
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_listing_and_version(
        self, listing_id: uuid.UUID, version: str
    ) -> Optional[MarketplacePackageVersion]:
        stmt = select(MarketplacePackageVersion).where(
            MarketplacePackageVersion.listing_id == listing_id,
            MarketplacePackageVersion.version == version,
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def get_latest_published(
        self, listing_id: uuid.UUID
    ) -> Optional[MarketplacePackageVersion]:
        stmt = (
            select(MarketplacePackageVersion)
            .where(
                MarketplacePackageVersion.listing_id == listing_id,
                MarketplacePackageVersion.status.in_(["published", "approved", "verified"]),
            )
            .order_by(MarketplacePackageVersion.created_at.desc())
            .limit(1)
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_listing(
        self, listing_id: uuid.UUID
    ) -> List[MarketplacePackageVersion]:
        stmt = (
            select(MarketplacePackageVersion)
            .where(MarketplacePackageVersion.listing_id == listing_id)
            .order_by(MarketplacePackageVersion.created_at.desc())
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all())

    async def create(self, version: MarketplacePackageVersion) -> MarketplacePackageVersion:
        self.db.add(version)
        await self.db.flush()
        return version
