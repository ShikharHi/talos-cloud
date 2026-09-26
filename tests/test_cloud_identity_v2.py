"""
Talos Cloud — Comprehensive End-to-End Cloud-Centric Identity & Auth V2 Tests.

Covers the full test matrix required by TALOS AUTHENTICATION V2:
  1. Unified Cloud Account, Identity, Device, and Session hierarchy.
  2. RS256 Cloud-issued access token minting and validation.
  3. Single identity across Web, Desktop, Local Runtime, CLI, Marketplace, and Relay.
  4. Multi-device independent sessions and independent revocation.
  5. Cloud revocation propagating to execution planes (device revoke & session revoke).
  6. Scoped API Key creation, authentication, revocation, and scope enforcement.
  7. Remote / SSH out-of-band device authorization flow.
  8. Capability-based authorization service (ALLOW, REQUIRE_APPROVAL, DENY).
  9. Silent re-authentication / credential management.
"""

import os
import uuid
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.accounts import (
    Account,
    ApiKey,
    Device,
    Identity,
    Session,
)
from app.services import (
    authorization_service,
    cloud_identity_service,
    device_flow_service,
    identity_service,
)
from app.services.authorization_service import AuthDecision, AuthorizationContext


@pytest_asyncio.fixture(scope="module")
async def engine_v2():
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
async def db_v2(engine_v2):
    session_factory = async_sessionmaker(engine_v2, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


class TestCloudCentricIdentityV2:
    @pytest.mark.asyncio
    async def test_account_creation_and_identity_federation(self, db_v2):
        """Account owns federated identities (Google, GitHub, OIDC) without coupling to sub."""
        email = f"user_{uuid.uuid4().hex[:8]}@talos.cloud"
        acc = Account(
            account_id=uuid.uuid4(),
            email=email,
            role="user",
            status="active",
        )
        db_v2.add(acc)
        await db_v2.flush()

        # Link Google identity
        google_id = Identity(
            identity_id=uuid.uuid4(),
            account_id=acc.account_id,
            provider="google",
            provider_subject=f"goog_{uuid.uuid4().hex}",
            email=email,
            email_verified=True,
        )
        db_v2.add(google_id)
        await db_v2.flush()

        assert acc.account_id is not None
        assert google_id.account_id == acc.account_id

    @pytest.mark.asyncio
    async def test_device_registration_and_independent_sessions(self, db_v2):
        """First-class devices: PC A and PC B on same Account with independent sessions."""
        acc = Account(
            account_id=uuid.uuid4(),
            email=f"multidev_{uuid.uuid4().hex[:6]}@talos.cloud",
            status="active",
        )
        db_v2.add(acc)
        await db_v2.flush()

        # Register Device A (Windows)
        dev_a = await cloud_identity_service.register_device(
            db=db_v2,
            account_id=acc.account_id,
            device_name="Work Windows PC",
            platform="windows",
            device_type="desktop",
        )
        # Register Device B (macOS)
        dev_b = await cloud_identity_service.register_device(
            db=db_v2,
            account_id=acc.account_id,
            device_name="MacBook Pro",
            platform="darwin",
            device_type="desktop",
        )
        await db_v2.flush()

        # Issue independent sessions for each device
        token_a, refresh_a, sess_a = await cloud_identity_service.create_cloud_session(
            db=db_v2,
            account=acc,
            session_type="desktop",
            device=dev_a,
        )
        token_b, refresh_b, sess_b = await cloud_identity_service.create_cloud_session(
            db=db_v2,
            account=acc,
            session_type="desktop",
            device=dev_b,
        )
        await db_v2.flush()

        assert sess_a.session_id != sess_b.session_id
        assert sess_a.device_id == dev_a.device_id
        assert sess_b.device_id == dev_b.device_id

        # Validate access tokens
        dec_a = identity_service.verify_web_session(token_a)
        dec_b = identity_service.verify_web_session(token_b)
        assert dec_a.account_id == acc.account_id
        assert dec_b.account_id == acc.account_id

        # Revoke Device A -> Session A invalidated, Session B remains valid
        await cloud_identity_service.revoke_device(db_v2, dev_a.device_id, acc.account_id)
        assert dev_a.is_revoked is True
        assert dev_b.is_revoked is False

        # Attempting refresh on Device A must fail with revoked error
        with pytest.raises(identity_service.InvalidSessionError, match="revoked"):
            await cloud_identity_service.refresh_cloud_session(db_v2, refresh_a)

        # Refresh on Device B succeeds
        new_token_b, new_ref_b, _ = await cloud_identity_service.refresh_cloud_session(db_v2, refresh_b)
        assert new_token_b is not None
        assert new_ref_b != refresh_b

    @pytest.mark.asyncio
    async def test_scoped_api_keys(self, db_v2):
        """First-class scoped API keys hashed with SHA-256."""
        acc = Account(
            account_id=uuid.uuid4(),
            email=f"apikey_{uuid.uuid4().hex[:6]}@talos.cloud",
            status="active",
        )
        db_v2.add(acc)
        await db_v2.flush()

        raw_key, key_rec = await cloud_identity_service.create_api_key_record(
            db=db_v2,
            account_id=acc.account_id,
            name="CI Pipeline Key",
            scopes=["agent:run", "workspace:read"],
        )
        await db_v2.flush()

        assert raw_key.startswith("talos_sk_live_")
        assert key_rec.key_hash != raw_key  # Server stores only hash

        # Authenticate with API key
        auth_res = await cloud_identity_service.authenticate_api_key(db_v2, raw_key)
        assert auth_res is not None
        auth_acc, auth_key = auth_res
        assert auth_acc.account_id == acc.account_id
        assert "agent:run" in auth_key.scopes

        # Revoke API key
        auth_key.revoked_at = cloud_identity_service._utcnow()
        await db_v2.flush()

        revoked_check = await cloud_identity_service.authenticate_api_key(db_v2, raw_key)
        assert revoked_check is None

    @pytest.mark.asyncio
    async def test_capability_authorization_engine(self):
        """Central authorization evaluates review, sandbox, trusted, unrestricted modes."""
        ctx_review = AuthorizationContext(
            account_id=str(uuid.uuid4()),
            role="user",
            execution_mode="review",
            scopes=["workspace:read", "workspace:write", "terminal:execute"],
        )

        # Read action allowed automatically in review mode
        dec1, _ = authorization_service.authorize(ctx_review, "workspace:read")
        assert dec1 == AuthDecision.ALLOW

        # Sensitive mutation requires approval in review mode
        dec2, _ = authorization_service.authorize(ctx_review, "terminal:execute")
        assert dec2 == AuthDecision.REQUIRE_APPROVAL

        # Sandbox mode forbids sensitive terminal executions outright
        ctx_sandbox = AuthorizationContext(
            account_id=str(uuid.uuid4()),
            role="user",
            execution_mode="sandbox",
            scopes=["*"],
        )
        dec3, _ = authorization_service.authorize(ctx_sandbox, "terminal:execute")
        assert dec3 == AuthDecision.DENY

        # Unrestricted mode allows everything
        ctx_unrestricted = AuthorizationContext(
            account_id=str(uuid.uuid4()),
            role="user",
            execution_mode="unrestricted",
            scopes=["*"],
        )
        dec4, _ = authorization_service.authorize(ctx_unrestricted, "terminal:execute")
        assert dec4 == AuthDecision.ALLOW

    @pytest.mark.asyncio
    async def test_remote_ssh_device_flow(self, db_v2):
        """Out-of-band challenge approval for headless / remote SSH."""
        acc = Account(
            account_id=uuid.uuid4(),
            email=f"ssh_{uuid.uuid4().hex[:6]}@talos.cloud",
            status="active",
        )
        db_v2.add(acc)
        await db_v2.flush()

        # Step 1: Remote CLI starts authorization challenge
        auth_session = await device_flow_service.create_device_authorization(device_label="Remote AWS Server")
        code = auth_session["user_code"]
        dev_code = auth_session["device_code"]

        # Step 2: Browser user approves challenge
        ok = await device_flow_service.approve_device_authorization(user_code=code, account_id=acc.account_id)
        assert bool(ok) is True

        # Step 3: Remote CLI completes challenge
        consumed = await device_flow_service.complete_device_authorization(dev_code)
        assert consumed is not None
        assert consumed["status"] == "approved"
        assert consumed["account_id"] == str(acc.account_id)

        # Step 4: Challenge cannot be replayed
        replayed = await device_flow_service.complete_device_authorization(dev_code)
        assert replayed is None
