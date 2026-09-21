"""
Talos Cloud — Package Security Analysis & Promotion Celery Tasks (Queue: package-security).

Runs sandboxed, asynchronous package static analysis and promotion:
  - Strict Zip-Slip and path traversal verification
  - AST dangerous module / call analysis
  - Decompression ratio / Zip bomb defenses
  - Idempotent promotion to canonical immutable production S3 keys
"""

import asyncio
import concurrent.futures
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app.app import celery_app
from app.database import get_session_factory
from app.models.marketplace import MarketplaceListing, MarketplacePackageVersion, PackageUpload
from app.storage.keys import package_object_key
from app.storage.service import get_storage_service

logger = logging.getLogger("talos.celery.security")


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


async def run_verify_and_promote_package(
    upload_id_str: str,
    db: AsyncSession | None = None,
    storage=None,
) -> dict[str, Any]:
    upload_uuid = uuid.UUID(upload_id_str)
    storage_svc = storage or get_storage_service()

    async def _process(session: AsyncSession):
        # 1. Fetch package upload record with row lock
        stmt = select(PackageUpload).where(PackageUpload.upload_id == upload_uuid).with_for_update()
        res = await session.execute(stmt)
        upload = res.scalar_one_or_none()

        if upload is None:
            logger.error("Upload %s not found for security verification", upload_id_str)
            return {"status": "not_found"}

        if upload.status == "promoted":
            logger.info("Upload %s already promoted (idempotent no-op)", upload_id_str)
            return {"status": "already_promoted"}


            # Mark state as VERIFYING
            upload.status = "verifying"
            await db.commit()

            canonical_key = package_object_key(upload.resource_type, upload.resource_id, upload.version)
            storage_svc = get_storage_service()

            # 2. Check if canonical key already exists (interrupted promotion idempotency)
            prod_exists = await storage_svc.exists(canonical_key)
            if prod_exists and upload.listing_id:
                ver_stmt = select(MarketplacePackageVersion).where(
                    MarketplacePackageVersion.listing_id == upload.listing_id,
                    MarketplacePackageVersion.version == upload.version,
                )
                ver_res = await db.execute(ver_stmt)
                if ver_res.scalar_one_or_none() is not None:
                    upload.status = "promoted"
                    await db.commit()
                    logger.info("Package %s version %s already promoted in storage and DB", upload.resource_id, upload.version)
                    return {"status": "promoted", "idempotent": True}

            # 3. Verify and promote via storage service
            try:
                verification = await storage_svc.verify_and_promote_package(
                    temp_key=upload.object_key,
                    canonical_key=canonical_key,
                    resource_type=upload.resource_type,
                    expected_sha256=upload.sha256,
                )
            except Exception as exc:
                logger.error("Security scan exception for upload %s: %s", upload_id_str, exc)
                upload.status = "failed"
                upload.failure_reason = str(exc)
                await db.commit()
                return {"status": "failed", "error": str(exc)}

            if not verification.valid:
                upload.status = "failed"
                upload.failure_reason = "; ".join(verification.errors)
                await db.commit()
                logger.warning("Package upload %s failed verification: %s", upload_id_str, upload.failure_reason)
                return {"status": "failed", "errors": verification.errors}

            # 4. Success -> Mark promoting and insert version record
            upload.status = "promoting"
            await db.commit()

            if upload.listing_id:
                # Insert or update package version row
                ver_stmt = select(MarketplacePackageVersion).where(
                    MarketplacePackageVersion.listing_id == upload.listing_id,
                    MarketplacePackageVersion.version == upload.version,
                )
                ver_res = await db.execute(ver_stmt)
                version_record = ver_res.scalar_one_or_none()

                if version_record is None:
                    version_record = MarketplacePackageVersion(
                        listing_id=upload.listing_id,
                        version=upload.version,
                        storage_key=canonical_key,
                        bucket=upload.bucket,
                        file_size=verification.file_size,
                        sha256=verification.sha256,
                        manifest_yaml=verification.manifest_yaml or "",
                        status="verified",
                    )
                    db.add(version_record)
                else:
                    version_record.storage_key = canonical_key
                    version_record.file_size = verification.file_size
                    version_record.sha256 = verification.sha256
                    version_record.manifest_yaml = verification.manifest_yaml or ""
                    version_record.status = "verified"

                # Update listing status to approved if currently pending
                list_stmt = select(MarketplaceListing).where(MarketplaceListing.listing_id == upload.listing_id)
                list_res = await db.execute(list_stmt)
                listing = list_res.scalar_one_or_none()
                if listing:
                    if listing.status == "pending":
                        listing.status = "approved"
                    listing.version = upload.version

            upload.status = "promoted"
            await session.commit()
            logger.info("Package %s version %s successfully verified and promoted", upload.resource_id, upload.version)
            return {"status": "promoted", "canonical_key": canonical_key}

    if db is not None:
        return await _process(db)

    factory = get_session_factory()
    async with factory() as session:
        return await _process(session)


@celery_app.task(name="app.celery_app.tasks.security.verify_and_promote_package_task", bind=True)
def verify_and_promote_package_task(self=None, upload_id_str: str = "", db: AsyncSession | None = None):
    """
    Asynchronous package verification and promotion worker.
    Runs on dedicated queue: package-security (concurrency 1).
    """
    result = _run_async(run_verify_and_promote_package(upload_id_str=upload_id_str, db=db))
    return result

