"""
Talos Cloud — Marketplace Upload Service.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.marketplace.errors import (
    ListingNotFoundError,
    PublishNotAllowedError,
    UploadAlreadyCompletedError,
    UploadExpiredError,
    UploadNotFoundError,
    UploadNotOwnedError,
    VersionConflictError,
)
from app.domain.marketplace.upload import UploadStatus
from app.infrastructure.storage.tigris import (
    TigrisMarketplaceStorage,
    staging_package_key,
)
from app.models.accounts import Account
from app.models.marketplace import PackageUpload
from app.repositories.marketplace.listing_repo import ListingRepository
from app.repositories.marketplace.upload_repo import UploadRepository
from app.repositories.marketplace.version_repo import VersionRepository


class UploadService:
    def __init__(self, db: AsyncSession, storage: Optional[TigrisMarketplaceStorage] = None):
        self.db = db
        self.upload_repo = UploadRepository(db)
        self.listing_repo = ListingRepository(db)
        self.version_repo = VersionRepository(db)
        self.storage = storage or TigrisMarketplaceStorage()

    async def init_upload(
        self,
        account: Account,
        listing_id: uuid.UUID,
        version: str,
        file_size: Optional[int] = None,
        sha256: Optional[str] = None,
        expires_in: int = 900,
    ) -> dict[str, Any]:
        listing = await self.listing_repo.get_by_id(listing_id)
        if not listing:
            raise ListingNotFoundError(f"Listing '{listing_id}' not found.")

        if listing.author_account_id != account.account_id and getattr(account, "role", "user") != "admin":
            raise PublishNotAllowedError(f"You are not authorized to publish new versions for '{listing.slug}'.")

        # Invariant: Reject duplicate published versions
        existing_ver = await self.version_repo.get_by_listing_and_version(listing_id, version)
        if existing_ver and existing_ver.status in ("published", "approved"):
            raise VersionConflictError(
                f"Version '{version}' for listing '{listing.slug}' is already published and immutable."
            )

        upload_id = uuid.uuid4()
        staging_key = staging_package_key(listing.kind, listing_id, upload_id)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # Generate short-lived presigned PUT URL
        upload_url = await self.storage.generate_presigned_upload_url(
            staging_key=staging_key,
            expires_in=expires_in,
            content_type="application/zip",
        )

        upload_record = PackageUpload(
            upload_id=upload_id,
            account_id=account.account_id,
            listing_id=listing_id,
            resource_type=listing.kind,
            resource_id=listing.slug,
            version=version,
            object_key=staging_key,
            staging_key=staging_key,
            bucket=self.storage._storage.default_bucket,
            expected_size=file_size,
            sha256=sha256,
            status=UploadStatus.PENDING.value,
            expires_at=expires_at,
        )
        await self.upload_repo.create(upload_record)

        return {
            "upload_id": str(upload_id),
            "upload_url": upload_url,
            "staging_key": staging_key,
            "expires_in": expires_in,
            "expires_at": expires_at.isoformat(),
        }

    async def complete_upload(
        self,
        account: Account,
        upload_id: uuid.UUID,
        async_verification: bool = True,
    ) -> dict[str, Any]:
        # Lock upload row
        upload = await self.upload_repo.get_by_id(upload_id, for_update=True)
        if not upload:
            raise UploadNotFoundError(f"Upload session '{upload_id}' not found.")

        if upload.account_id != account.account_id:
            raise UploadNotOwnedError("You do not own this upload session.")

        # Idempotent status check
        if upload.status == UploadStatus.PROMOTED.value:
            return {"status": "promoted", "upload_id": str(upload_id), "idempotent": True}
        if upload.status == UploadStatus.VERIFYING.value:
            return {"status": "verifying", "upload_id": str(upload_id)}
        if upload.status == UploadStatus.FAILED.value:
            return {"status": "failed", "upload_id": str(upload_id), "reason": upload.failure_reason}

        # Check expiry
        now = datetime.now(timezone.utc)
        exp = upload.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if now > exp:
            upload.status = UploadStatus.EXPIRED.value
            await self.db.flush()
            raise UploadExpiredError("Upload session has expired.")

        staging_key = upload.staging_key or upload.object_key
        # Verify object existence in Tigris S3
        exists = await self.storage.exists(staging_key)
        if not exists:
            raise UploadNotFoundError(
                f"Staging object '{staging_key}' was not found in storage. Did the direct PUT upload complete?"
            )

        upload.status = UploadStatus.VERIFYING.value
        await self.db.flush()

        if async_verification:
            try:
                import inngest
                from app.inngest.client import inngest_client
                from app.inngest.events import TalosEvents

                await inngest_client.send(
                    inngest.Event(
                        name=TalosEvents.MARKETPLACE_PACKAGE_UPLOADED,
                        data={
                            "upload_id": str(upload_id),
                            "account_id": str(account.account_id),
                        },
                    )
                )
                return {"status": "verifying", "upload_id": str(upload_id), "async": True}
            except Exception as e:
                logger.warning(
                    "Inngest dispatch failed (%s). Executing async_verify_and_promote directly.", e
                )
                from app.celery_app.tasks.marketplace import async_verify_and_promote
                return await async_verify_and_promote(str(upload_id), db=self.db, storage=self.storage)
        else:
            from app.celery_app.tasks.marketplace import async_verify_and_promote
            return await async_verify_and_promote(str(upload_id), db=self.db, storage=self.storage)

    async def get_upload_status(self, account: Account, upload_id: uuid.UUID) -> dict[str, Any]:
        upload = await self.upload_repo.get_by_id(upload_id)
        if not upload:
            raise UploadNotFoundError(f"Upload '{upload_id}' not found.")
        if upload.account_id != account.account_id:
            raise UploadNotOwnedError("You do not have permission to view this upload.")

        return {
            "upload_id": str(upload.upload_id),
            "status": upload.status,
            "failure_reason": upload.failure_reason,
            "created_at": upload.created_at.isoformat() if upload.created_at else None,
            "verified_at": upload.verified_at.isoformat() if upload.verified_at else None,
            "promoted_at": upload.promoted_at.isoformat() if upload.promoted_at else None,
        }
