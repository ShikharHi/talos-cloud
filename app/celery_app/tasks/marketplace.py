"""
Talos Cloud — Marketplace Celery Tasks (Queue: package-security & maintenance).

Handles asynchronous package verification, security scanning, promotion to Tigris,
and orphan staging cleanup.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app.app import celery_app
from app.database import get_session_factory
from app.domain.marketplace.errors import MarketplaceError
from app.domain.marketplace.security import SecurityScanReport
from app.domain.marketplace.upload import UploadStatus
from app.domain.marketplace.version import SecurityValidationStatus, VersionStatus
from app.infrastructure.security.package_scanner import PackageScanner
from app.infrastructure.storage.tigris import (
    TigrisMarketplaceStorage,
    canonical_package_key,
)
from app.models.marketplace import (
    MarketplaceListing,
    MarketplacePackageVersion,
    PackageUpload,
)

logger = logging.getLogger("talos.celery.marketplace")


def _run_async(coro):
    """Safely run async coroutine from sync Celery worker."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


async def async_verify_and_promote(
    upload_id_str: str,
    db: AsyncSession | None = None,
    storage: TigrisMarketplaceStorage | None = None,
) -> dict[str, Any]:
    """
    Asynchronously executes full package security scan and idempotent promotion:
      1. Claims upload row with with_for_update()
      2. Downloads staging zip from Tigris
      3. Verifies zip integrity, paths (Zip Slip), limits, manifest, and AST policy
      4. Idempotently copies object to canonical immutable production S3 key
      5. Commits MarketplacePackageVersion row and updates listing version
      6. Transitions upload state to PROMOTED
    """
    upload_uuid = uuid.UUID(upload_id_str)
    storage_client = storage or TigrisMarketplaceStorage()

    session_factory = get_session_factory()

    async def _execute(session: AsyncSession) -> dict[str, Any]:
        # 1. Fetch package upload with row lock
        stmt = select(PackageUpload).where(PackageUpload.upload_id == upload_uuid).with_for_update()
        res = await session.execute(stmt)
        upload = res.scalar_one_or_none()

        if upload is None:
            logger.error("Upload %s not found for processing", upload_id_str)
            return {"status": "not_found", "upload_id": upload_id_str}

        if upload.status == UploadStatus.PROMOTED.value:
            logger.info("Upload %s already promoted (idempotent no-op)", upload_id_str)
            return {"status": "promoted", "upload_id": upload_id_str, "idempotent": True}

        # 2. Mark state as VERIFYING
        upload.status = UploadStatus.VERIFYING.value
        await session.commit()

        staging_key = upload.staging_key or upload.object_key
        listing_id = upload.listing_id

        # 3. Check listing
        if not listing_id:
            upload.status = UploadStatus.FAILED.value
            upload.failure_reason = "Upload is not associated with any listing."
            await session.commit()
            return {"status": "failed", "reason": upload.failure_reason}

        listing_stmt = select(MarketplaceListing).where(MarketplaceListing.listing_id == listing_id)
        listing_res = await session.execute(listing_stmt)
        listing = listing_res.scalar_one_or_none()

        if not listing:
            upload.status = UploadStatus.FAILED.value
            upload.failure_reason = f"Associated listing {listing_id} not found."
            await session.commit()
            return {"status": "failed", "reason": upload.failure_reason}

        canonical_key = canonical_package_key(listing.kind, listing.listing_id, upload.version)

        # 4. Download staging package into temporary file for bounded verification
        report: SecurityScanReport
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
                bytes_downloaded = await storage_client.download_to_file(staging_key, tmp)
                tmp.seek(0)
                report = PackageScanner.scan_archive_stream(
                    stream_file=tmp,
                    kind=listing.kind,
                    expected_sha256=upload.sha256,
                )
        except Exception as exc:
            logger.warning("Security verification failed for upload %s: %s", upload_id_str, exc)
            upload.status = UploadStatus.FAILED.value
            upload.failure_reason = str(exc)
            await session.commit()
            return {"status": "failed", "reason": str(exc)}

        upload.status = UploadStatus.VERIFIED.value
        upload.verified_at = datetime.now(timezone.utc)
        await session.commit()

        # 5. Idempotent promotion to canonical release key in Tigris S3
        upload.status = UploadStatus.PROMOTING.value
        await session.commit()

        try:
            await storage_client.promote_staging_to_canonical(
                staging_key=staging_key,
                canonical_key=canonical_key,
                expected_sha256=report.sha256,
            )
        except Exception as exc:
            logger.error("Promotion failed for upload %s: %s", upload_id_str, exc)
            upload.status = UploadStatus.FAILED.value
            upload.failure_reason = f"Storage promotion failed: {exc}"
            await session.commit()
            return {"status": "failed", "reason": upload.failure_reason}

        # 6. Create immutable MarketplacePackageVersion record
        ver_stmt = select(MarketplacePackageVersion).where(
            MarketplacePackageVersion.listing_id == listing.listing_id,
            MarketplacePackageVersion.version == upload.version,
        )
        ver_res = await session.execute(ver_stmt)
        existing_ver = ver_res.scalar_one_or_none()

        sec_status = SecurityValidationStatus.PASSED.value if not report.has_critical_findings else SecurityValidationStatus.FLAGGED.value

        if existing_ver is None:
            new_version = MarketplacePackageVersion(
                listing_id=listing.listing_id,
                version=upload.version,
                storage_key=canonical_key,
                bucket=upload.bucket,
                file_size=report.file_size,
                sha256=report.sha256,
                manifest_yaml=listing.manifest_yaml,
                manifest_json=report.manifest_data,
                security_report=report.to_dict(),
                security_status=sec_status,
                status=VersionStatus.PUBLISHED.value,
                published_at=datetime.now(timezone.utc),
            )
            session.add(new_version)

        # Update listing display info from manifest if available
        if report.manifest_data:
            m = report.manifest_data
            if m.get("name") and not listing.display_name:
                listing.display_name = m["name"]
            if m.get("description") and not listing.tagline:
                listing.tagline = m["description"][:300]
        listing.version = upload.version
        if listing.status == "pending_review":
            listing.status = "approved"

        upload.status = UploadStatus.PROMOTED.value
        upload.promoted_at = datetime.now(timezone.utc)
        await session.commit()

        # 7. Clean up staging object
        try:
            await storage_client.delete(staging_key)
        except Exception as e:
            logger.warning("Could not delete staging object %s: %s", staging_key, e)

        logger.info("Upload %s successfully verified and promoted to %s", upload_id_str, canonical_key)
        return {"status": "promoted", "upload_id": upload_id_str, "canonical_key": canonical_key}

    if db is not None:
        return await _execute(db)
    else:
        async with session_factory() as session:
            return await _execute(session)


@celery_app.task(
    name="app.celery_app.tasks.marketplace.verify_and_promote_package_task",
    queue="package-security",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def verify_and_promote_package_task(self, upload_id_str: str) -> dict[str, Any]:
    try:
        return _run_async(async_verify_and_promote(upload_id_str))
    except Exception as exc:
        logger.error("verify_and_promote_package_task error: %s", exc)
        raise self.retry(exc=exc)


@celery_app.task(
    name="app.celery_app.tasks.marketplace.cleanup_expired_uploads_task",
    queue="maintenance",
)
def cleanup_expired_uploads_task() -> int:
    """Finds expired staging uploads, deletes their S3 staging objects, and marks them EXPIRED."""
    async def _cleanup():
        session_factory = get_session_factory()
        storage_client = TigrisMarketplaceStorage()
        cleaned = 0
        now = datetime.now(timezone.utc)

        async with session_factory() as session:
            stmt = select(PackageUpload).where(
                PackageUpload.status.in_([UploadStatus.PENDING.value, UploadStatus.UPLOADING.value, UploadStatus.FAILED.value]),
                PackageUpload.expires_at < now,
            ).limit(100)
            res = await session.execute(stmt)
            expired_uploads = res.scalars().all()

            for upl in expired_uploads:
                staging_key = upl.staging_key or upl.object_key
                try:
                    if await storage_client.exists(staging_key):
                        await storage_client.delete(staging_key)
                except Exception as e:
                    logger.warning("Error deleting expired staging key %s: %s", staging_key, e)

                upl.status = UploadStatus.EXPIRED.value
                cleaned += 1

            await session.commit()
        return cleaned

    return _run_async(_cleanup())
