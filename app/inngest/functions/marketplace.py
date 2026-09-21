"""
Talos Cloud — Marketplace Inngest Functions.

Implements durable, multi-step asynchronous package verification,
security scanning, Tigris promotion, and staging cleanup.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any

import inngest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session_factory
from app.domain.marketplace.security import SecurityScanReport
from app.domain.marketplace.upload import UploadStatus
from app.domain.marketplace.version import SecurityValidationStatus, VersionStatus
from app.infrastructure.security.package_scanner import PackageScanner
from app.infrastructure.storage.tigris import (
    TigrisMarketplaceStorage,
    canonical_package_key,
)
from app.inngest.client import inngest_client
from app.inngest.events import TalosEvents
from app.models.marketplace import (
    MarketplaceListing,
    MarketplacePackageVersion,
    PackageUpload,
)

logger = logging.getLogger("talos.inngest.marketplace")


@inngest_client.create_function(
    fn_id="talos.marketplace.verify_and_promote",
    name="Talos Marketplace: Verify & Promote Package",
    trigger=inngest.TriggerEvent(event=TalosEvents.MARKETPLACE_PACKAGE_UPLOADED),
    retries=3,
    concurrency=[
        inngest.Concurrency(
            scope="fn",
            limit=2,
        )
    ],
)
async def marketplace_verify_and_promote_fn(
    ctx: inngest.Context,
    step: inngest.Step,
) -> dict[str, Any]:
    upload_id_str = ctx.event.data.get("upload_id")
    if not upload_id_str:
        raise inngest.NonRetriableError("Missing upload_id in event data")

    upload_uuid = uuid.UUID(upload_id_str)
    session_factory = get_session_factory()
    storage_client = TigrisMarketplaceStorage()

    # Step 1: Claim and verify archive
    async def _step_claim_and_verify() -> dict[str, Any]:
        async with session_factory() as session:
            stmt = select(PackageUpload).where(PackageUpload.upload_id == upload_uuid).with_for_update()
            res = await session.execute(stmt)
            upload = res.scalar_one_or_none()

            if upload is None:
                raise inngest.NonRetriableError(f"Upload {upload_id_str} not found in database")

            if upload.status == UploadStatus.PROMOTED.value:
                return {
                    "already_promoted": True,
                    "upload_id": upload_id_str,
                }

            upload.status = UploadStatus.VERIFYING.value
            await session.commit()

            staging_key = upload.staging_key or upload.object_key
            listing_id = upload.listing_id

            if not listing_id:
                upload.status = UploadStatus.FAILED.value
                upload.failure_reason = "Upload is not associated with any listing."
                await session.commit()
                raise inngest.NonRetriableError(upload.failure_reason)

            listing_stmt = select(MarketplaceListing).where(MarketplaceListing.listing_id == listing_id)
            listing_res = await session.execute(listing_stmt)
            listing = listing_res.scalar_one_or_none()
            if not listing:
                upload.status = UploadStatus.FAILED.value
                upload.failure_reason = f"Associated listing {listing_id} not found."
                await session.commit()
                raise inngest.NonRetriableError(upload.failure_reason)

            canonical_key = canonical_package_key(listing.kind, listing.listing_id, upload.version)

            try:
                with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
                    await storage_client.download_to_file(staging_key, tmp)
                    tmp.seek(0)
                    report = PackageScanner.scan_archive_stream(
                        stream_file=tmp,
                        kind=listing.kind,
                        expected_sha256=upload.sha256,
                    )
            except Exception as exc:
                upload.status = UploadStatus.FAILED.value
                upload.failure_reason = str(exc)
                await session.commit()
                raise inngest.NonRetriableError(f"Archive scan failed: {exc}")

            upload.status = UploadStatus.VERIFIED.value
            upload.verified_at = datetime.now(timezone.utc)
            await session.commit()

            return {
                "already_promoted": False,
                "upload_id": upload_id_str,
                "staging_key": staging_key,
                "canonical_key": canonical_key,
                "version": upload.version,
                "listing_id": str(listing.listing_id),
                "listing_kind": listing.kind,
                "report": report.to_dict(),
                "sha256": report.sha256,
                "file_size": report.file_size,
                "has_critical": report.has_critical_findings,
                "manifest_data": report.manifest_data,
            }

    verify_result = await step.run("claim-and-verify-archive", _step_claim_and_verify)

    if verify_result.get("already_promoted"):
        return {"status": "promoted", "upload_id": upload_id_str, "idempotent": True}

    staging_key = verify_result["staging_key"]
    canonical_key = verify_result["canonical_key"]
    sha256 = verify_result["sha256"]

    # Step 2: Idempotent promotion in Tigris S3
    async def _step_promote_to_canonical() -> dict[str, Any]:
        async with session_factory() as session:
            stmt = select(PackageUpload).where(PackageUpload.upload_id == upload_uuid)
            res = await session.execute(stmt)
            upload = res.scalar_one_or_none()
            if upload:
                upload.status = UploadStatus.PROMOTING.value
                await session.commit()

        try:
            await storage_client.promote_staging_to_canonical(
                staging_key=staging_key,
                canonical_key=canonical_key,
                expected_sha256=sha256,
            )
        except Exception as exc:
            async with session_factory() as session:
                stmt = select(PackageUpload).where(PackageUpload.upload_id == upload_uuid)
                res = await session.execute(stmt)
                upload = res.scalar_one_or_none()
                if upload:
                    upload.status = UploadStatus.FAILED.value
                    upload.failure_reason = f"Storage promotion failed: {exc}"
                    await session.commit()
            raise

        return {"promoted_canonical_key": canonical_key}

    await step.run("promote-to-canonical-storage", _step_promote_to_canonical)

    # Step 3: Record version in PostgreSQL & mark promoted
    async def _step_commit_database_records() -> dict[str, Any]:
        async with session_factory() as session:
            stmt = select(PackageUpload).where(PackageUpload.upload_id == upload_uuid).with_for_update()
            res = await session.execute(stmt)
            upload = res.scalar_one_or_none()
            if not upload:
                raise inngest.NonRetriableError(f"Upload {upload_id_str} disappeared during commit")

            listing_id = upload.listing_id
            listing_stmt = select(MarketplaceListing).where(MarketplaceListing.listing_id == listing_id).with_for_update()
            listing_res = await session.execute(listing_stmt)
            listing = listing_res.scalar_one_or_none()

            ver_stmt = select(MarketplacePackageVersion).where(
                MarketplacePackageVersion.listing_id == listing_id,
                MarketplacePackageVersion.version == upload.version,
            )
            ver_res = await session.execute(ver_stmt)
            existing_ver = ver_res.scalar_one_or_none()

            sec_status = (
                SecurityValidationStatus.PASSED.value
                if not verify_result.get("has_critical")
                else SecurityValidationStatus.FLAGGED.value
            )

            manifest_data = verify_result.get("manifest_data") or {}

            if existing_ver is None:
                new_version = MarketplacePackageVersion(
                    listing_id=listing_id,
                    version=upload.version,
                    storage_key=canonical_key,
                    bucket=upload.bucket,
                    file_size=verify_result["file_size"],
                    sha256=sha256,
                    manifest_yaml=listing.manifest_yaml if listing else "",
                    manifest_json=manifest_data,
                    security_report=verify_result.get("report") or {},
                    security_status=sec_status,
                    status=VersionStatus.PUBLISHED.value,
                    published_at=datetime.now(timezone.utc),
                )
                session.add(new_version)

            if listing:
                if manifest_data:
                    if manifest_data.get("name") and not listing.display_name:
                        listing.display_name = manifest_data["name"]
                    if manifest_data.get("description") and not listing.tagline:
                        listing.tagline = manifest_data["description"][:300]
                listing.version = upload.version
                if listing.status == "pending_review":
                    listing.status = "approved"

            upload.status = UploadStatus.PROMOTED.value
            upload.promoted_at = datetime.now(timezone.utc)
            await session.commit()
            return {"committed": True}

    await step.run("commit-database-records", _step_commit_database_records)

    # Step 4: Clean up staging object
    async def _step_delete_staging_object() -> dict[str, Any]:
        try:
            await storage_client.delete(staging_key)
        except Exception as e:
            logger.warning("Could not delete staging object %s: %s", staging_key, e)
        return {"deleted_staging": True}

    await step.run("delete-staging-object", _step_delete_staging_object)

    return {
        "status": "promoted",
        "upload_id": upload_id_str,
        "canonical_key": canonical_key,
    }
