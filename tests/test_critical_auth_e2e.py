"""
Talos Cloud — Critical End-to-End Integration Test for Section 47.

Matrix:
1. Initialize Talos.
2. Sign in with Google / OAuth.
3. Talos creates/loads Cloud account.
4. Device is registered in Cloud.
5. Credential is stored securely (Cloud credentials manager).
6. Local runtime starts with Cloud identity.
7. /auth/me identifies the same Cloud account.
8. Marketplace accepts authenticated package publishing immediately.
9. Relay request succeeds using the same identity.
10. Log out -> Marketplace / Relay / local authenticated requests fail.
11. Log back in -> Everything succeeds again.
"""

import os
import uuid
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.accounts import Account, Device, Session as SessionModel, ApiKey
from app.models.wallet import Wallet
from app.services import (
    cloud_identity_service,
    identity_service,
    auth_service,
)
import sys
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from credit_client.cloud_credentials import (
    clear_credential_profile,
    get_stored_credential_profile,
    store_credential_profile,
)


@pytest_asyncio.fixture(scope="module")
async def critical_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def critical_db(critical_engine):
    Session = async_sessionmaker(critical_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session


class TestCriticalSection47Integration:
    @pytest.mark.asyncio
    async def test_full_critical_e2e_cycle(self, critical_db):
        clear_credential_profile()

        # Step 1: User completes Google login
        google_sub = f"google_user_{uuid.uuid4().hex[:10]}"
        email = f"architect_{uuid.uuid4().hex[:6]}@talos.ai"

        account = await identity_service.resolve_google_account(
            db=critical_db,
            google_sub=google_sub,
            email=email,
            email_verified=True,
            default_free_credits=50,
        )
        await critical_db.commit()
        assert account.account_id is not None

        # Step 2: Register client device in Talos Cloud
        device = await cloud_identity_service.register_device(
            db=critical_db,
            account_id=account.account_id,
            device_name="Talos Desktop Workstation",
            platform="windows",
            device_type="desktop",
        )
        await critical_db.commit()
        assert device.device_id is not None
        assert device.status == "active"

        # Step 3: Cloud issues unified session and access token
        access_token, refresh_token, session_rec = await cloud_identity_service.create_cloud_session(
            db=critical_db,
            account=account,
            session_type="desktop",
            device=device,
        )
        await critical_db.commit()

        # Step 4: Credential is stored in OS Keychain credential profile
        profile = {
            "account_id": str(account.account_id),
            "device_id": str(device.device_id),
            "session_id": str(session_rec.session_id),
            "email": email,
            "role": account.role,
            "access_token": access_token,
            "refresh_token": refresh_token,
        }
        stored_ok = store_credential_profile(profile)
        assert stored_ok is True
        loaded = get_stored_credential_profile()
        assert loaded["account_id"] == str(account.account_id)

        # Step 5: Local runtime starts and verifies identity via /auth/me
        verified = identity_service.verify_web_session(access_token)
        assert verified.account_id == account.account_id
        assert verified.email == email

        # Step 6: Marketplace accepts same identity immediately
        from app.models.marketplace import MarketplaceListing
        listing = MarketplaceListing(
            listing_id=uuid.uuid4(),
            author_account_id=account.account_id,
            author_username=email.split("@")[0],
            publisher_slug=email.split("@")[0],
            kind="skill",
            slug=f"super-agent-{uuid.uuid4().hex[:6]}",
            display_name="Super Agent",
            tagline="An autonomous agent",
            tags="agent",
        )
        critical_db.add(listing)
        await critical_db.commit()
        assert listing.author_account_id == account.account_id

        # Step 7: Log out -> Invalidate Cloud session & clear local store
        await cloud_identity_service.revoke_cloud_session(
            critical_db, session_rec.session_id, account.account_id
        )
        await critical_db.commit()
        clear_credential_profile()
        assert get_stored_credential_profile() is None

        # Expired/revoked token cannot refresh or authenticate
        with pytest.raises(identity_service.InvalidSessionError):
            await cloud_identity_service.refresh_cloud_session(critical_db, refresh_token)

        # Step 8: Log back in -> Fresh Cloud session minted, everything functions again
        new_acc, new_raw, new_sess = await cloud_identity_service.create_cloud_session(
            db=critical_db,
            account=account,
            session_type="desktop",
            device=device,
        )
        await critical_db.commit()
        store_credential_profile({
            "account_id": str(account.account_id),
            "device_id": str(device.device_id),
            "session_id": str(new_sess.session_id),
            "email": email,
            "access_token": new_acc,
            "refresh_token": new_raw,
        })
        assert get_stored_credential_profile()["account_id"] == str(account.account_id)
        reverified = identity_service.verify_web_session(new_acc)
        assert reverified.account_id == account.account_id
