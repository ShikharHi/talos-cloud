"""
Talos Cloud — Marketplace Installation Repository.
"""

from __future__ import annotations

import uuid
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.marketplace import UserInstall


class InstallRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_account_and_listing(
        self, account_id: uuid.UUID, listing_id: uuid.UUID
    ) -> Optional[UserInstall]:
        stmt = select(UserInstall).where(
            UserInstall.account_id == account_id,
            UserInstall.listing_id == listing_id,
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_token(self, install_token: str) -> Optional[UserInstall]:
        stmt = select(UserInstall).where(UserInstall.install_token == install_token)
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_account(
        self, account_id: uuid.UUID, status: str = "active"
    ) -> List[UserInstall]:
        stmt = (
            select(UserInstall)
            .where(
                UserInstall.account_id == account_id,
                UserInstall.status == status,
            )
            .options(selectinload(UserInstall.listing))
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all())

    async def create(self, install: UserInstall) -> UserInstall:
        self.db.add(install)
        await self.db.flush()
        return install
