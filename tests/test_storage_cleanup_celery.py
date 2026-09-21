"""
Unit tests for the Celery Beat background storage cleanup task.
"""

import uuid
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from app.models.accounts import Account
from app.models.marketplace import PackageUpload
from app.services.storage_cleanup import cleanup_abandoned_uploads
from app.storage.models import UploadState
from app.storage.service import StorageService
from tests.test_storage_provider import FakeStorageProvider


@pytest.mark.asyncio
async def test_cleanup_abandoned_uploads(db_session):
    """
    Verifies that abandoned/expired pending uploads are purged from storage
    and marked as 'expired', while active and completed uploads remain untouched.
    """
    account = Account(email="cleanup_test@example.com", role="user")
    db_session.add(account)
    await db_session.commit()

    provider = FakeStorageProvider(default_bucket="talos-marketplace")
    storage = StorageService(provider=provider, default_bucket="talos-marketplace")

    now = datetime.now(timezone.utc)

    # 1. Stale pending upload (expired 10 minutes ago)
    stale_upload_id = uuid.uuid4()
    stale_key = f"uploads/agents/stale/{stale_upload_id}/package.zip"
    await provider.upload(stale_key, b"stale-data", "application/zip")

    stale_rec = PackageUpload(
        upload_id=stale_upload_id,
        account_id=account.account_id,
        resource_type="agents",
        resource_id="stale-bot",
        version="1.0.0",
        object_key=stale_key,
        bucket="talos-marketplace",
        status=UploadState.PENDING.value,
        expires_at=now - timedelta(minutes=10),
    )
    db_session.add(stale_rec)

    # 2. Active pending upload (expires in 10 minutes)
    active_upload_id = uuid.uuid4()
    active_key = f"uploads/agents/active/{active_upload_id}/package.zip"
    await provider.upload(active_key, b"active-data", "application/zip")

    active_rec = PackageUpload(
        upload_id=active_upload_id,
        account_id=account.account_id,
        resource_type="agents",
        resource_id="active-bot",
        version="1.0.0",
        object_key=active_key,
        bucket="talos-marketplace",
        status=UploadState.PENDING.value,
        expires_at=now + timedelta(minutes=10),
    )
    db_session.add(active_rec)

    # 3. Completed upload (already published)
    completed_upload_id = uuid.uuid4()
    comp_rec = PackageUpload(
        upload_id=completed_upload_id,
        account_id=account.account_id,
        resource_type="agents",
        resource_id="comp-bot",
        version="1.0.0",
        object_key="agents/comp-bot/1.0.0/package.zip",
        bucket="talos-marketplace",
        status=UploadState.COMPLETED.value,
        expires_at=now - timedelta(hours=1),
    )
    db_session.add(comp_rec)
    await db_session.commit()

    # Run cleanup
    stats = await cleanup_abandoned_uploads(db_session, storage=storage)

    assert stats["stale_found"] == 1
    assert stats["storage_deleted"] == 1
    assert stats["expired_marked"] == 1

    # Verify stale record in DB is now EXPIRED
    refreshed_stale = (await db_session.execute(
        select(PackageUpload).where(PackageUpload.upload_id == stale_upload_id)
    )).scalars().first()
    assert refreshed_stale.status == UploadState.EXPIRED.value

    # Verify stale object is gone from storage
    assert await provider.exists(stale_key) is False

    # Verify active record is still PENDING and object still exists
    refreshed_active = (await db_session.execute(
        select(PackageUpload).where(PackageUpload.upload_id == active_upload_id)
    )).scalars().first()
    assert refreshed_active.status == UploadState.PENDING.value
    assert await provider.exists(active_key) is True

    # Verify completed record untouched
    refreshed_comp = (await db_session.execute(
        select(PackageUpload).where(PackageUpload.upload_id == completed_upload_id)
    )).scalars().first()
    assert refreshed_comp.status == UploadState.COMPLETED.value
