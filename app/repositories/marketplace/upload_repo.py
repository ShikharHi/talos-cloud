"""
Talos Cloud — Marketplace Upload Session Repository.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketplace import PackageUpload


class UploadRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(
        self, upload_id: uuid.UUID, for_update: bool = False
    ) -> Optional[PackageUpload]:
        stmt = select(PackageUpload).where(PackageUpload.upload_id == upload_id)
        if for_update:
            stmt = stmt.with_for_update()
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def create(self, upload: PackageUpload) -> PackageUpload:
        self.db.add(upload)
        await self.db.flush()
        return upload

    async def list_expired_pending(
        self, cutoff: Optional[datetime] = None
    ) -> List[PackageUpload]:
        now = cutoff or datetime.now(timezone.utc)
        stmt = select(PackageUpload).where(
            PackageUpload.status.in_(["pending", "uploading"]),
            PackageUpload.expires_at < now,
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all())
