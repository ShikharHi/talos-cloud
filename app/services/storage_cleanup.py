"""
Talos Cloud — Storage Cleanup Service.

Handles cleanup of abandoned / expired temporary uploads and their staging objects in Tigris.
Idempotent and safe to retry.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketplace import PackageUpload
from app.storage.models import UploadState
from app.storage.service import StorageService, get_storage_service

logger = logging.getLogger("talos.storage.cleanup")


async def cleanup_abandoned_uploads(
    db: AsyncSession,
    storage: Optional[StorageService] = None,
) -> dict[str, int]:
    """
    Finds package_uploads records in 'pending' status whose expires_at has passed.
    For each record:
      1. Deletes the temporary staging object from storage (uploads/...)
      2. Marks upload status as 'expired'
    Returns summary dictionary with counts.
    """
    now = datetime.now(timezone.utc)
    stmt = select(PackageUpload).where(
        PackageUpload.status == UploadState.PENDING.value,
    )
    result = await db.execute(stmt)
    pending_uploads = list(result.scalars().all())

    stale_uploads = []
    for u in pending_uploads:
        exp = u.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            stale_uploads.append(u)

    if not stale_uploads:
        return {"stale_found": 0, "storage_deleted": 0, "expired_marked": 0}

    svc = storage or get_storage_service()
    deleted_objects = 0
    expired_count = 0

    for upload in stale_uploads:
        # Delete staging object from storage
        try:
            if upload.object_key and await svc.exists(upload.object_key):
                await svc.delete(upload.object_key)
                deleted_objects += 1
        except Exception as e:
            logger.warning(
                "Failed to delete storage object '%s' for expired upload %s: %s",
                upload.object_key,
                upload.upload_id,
                e,
            )

        # Update database record
        upload.status = UploadState.EXPIRED.value
        expired_count += 1

    await db.commit()

    logger.info(
        "Storage cleanup finished: found=%d, deleted_objects=%d, marked_expired=%d",
        len(stale_uploads),
        deleted_objects,
        expired_count,
    )

    return {
        "stale_found": len(stale_uploads),
        "storage_deleted": deleted_objects,
        "expired_marked": expired_count,
    }
