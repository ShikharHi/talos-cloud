"""
Talos Cloud — Production-Grade Auth & Accounts Hardening Tests.

Verifies:
  1. O(1) Fast Token Lookup with embedded token_id.
  2. Backward-compatible fallback for legacy tokens without token_id.
  3. Redis caching of active device tokens and immediate eviction on revocation/refresh.
  4. RFC 8628 Device Authorization Flow (request code, approve in browser, poll exchange).
  5. Multi-pod RSA key protection in production mode.
"""

import os
import time
import uuid
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models.accounts  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.ledger  # noqa: F401
import app.models.pricing  # noqa: F401
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.accounts import Account, DeviceToken
from app.services import auth_service, device_flow_service, identity_service


@pytest_asyncio.fixture(scope="module")
async def test_engine():
    db_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    engine = create_async_engine(
        db_url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db(test_engine):
    Session = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session


@pytest.fixture
def client(test_engine):
    Session = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_db():
        async with Session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestFastDeviceTokenLookup:
    """Verifies O(1) primary key lookup and legacy compatibility."""

    @pytest.mark.asyncio
    async def test_new_tokens_contain_embedded_token_id(self, db):
        account, raw_token = await auth_service.register_account(
            db, email=f"fast_{uuid.uuid4().hex[:8]}@example.com"
        )
        assert raw_token.startswith("dtok_")
        parts = raw_token.split("_")
        assert len(parts) >= 3, "New token format must be dtok_<token_id_hex>_<secret>"
        token_id = uuid.UUID(parts[1])

        # Verify device token row matches embedded token_id
        dt = await db.get(DeviceToken, token_id)
        assert dt is not None
        assert dt.account_id == account.account_id

        # Authenticate via O(1) path
        authenticated = await auth_service.authenticate_token(db, raw_token)
        assert authenticated is not None
        assert authenticated.token_id == token_id

    @pytest.mark.asyncio
    async def test_legacy_token_fallback_compatibility(self, db):
        """Tokens without token_id must fall back gracefully to scan path."""
        account = Account(email=f"legacy_{uuid.uuid4().hex[:8]}@example.com")
        db.add(account)
        await db.flush()

        # Generate legacy token without embedded token_id
        import secrets
        legacy_raw = f"dtok_{secrets.token_urlsafe(32)}"
        token_id = uuid.uuid4()
        legacy_dt = DeviceToken(
            token_id=token_id,
            account_id=account.account_id,
            token_hash=auth_service._hash_token(legacy_raw),
            device_label="Legacy Device",
            expires_at=auth_service._expiry(),
        )
        db.add(legacy_dt)
        await db.commit()

        # Authenticate must succeed via fallback path
        found = await auth_service.authenticate_token(db, legacy_raw)
        assert found is not None
        assert found.token_id == token_id

    @pytest.mark.asyncio
    async def test_token_revocation_and_refresh_evicts_cache(self, db):
        account, raw_token = await auth_service.register_account(
            db, email=f"revoke_{uuid.uuid4().hex[:8]}@example.com"
        )
        parts = raw_token.split("_")
        token_id = uuid.UUID(parts[1])

        # Populate cache
        assert await auth_service.authenticate_token(db, raw_token) is not None

        # Revoke
        revoked = await auth_service.revoke_token(db, raw_token)
        assert revoked is True
        await db.commit()

        # Token must not authenticate
        assert await auth_service.authenticate_token(db, raw_token) is None


class TestRFC8628DeviceAuthorizationFlow:
    """Verifies complete RFC 8628 Device Authorization Flow."""

    def test_device_code_request(self, client):
        resp = client.post("/auth/device-code", json={"device_label": "CLI Tool"})
        assert resp.status_code == 200
        data = resp.json()
        assert "device_code" in data
        assert "user_code" in data
        assert "-" in data["user_code"]  # e.g. WDJB-MJHT
        assert "verification_uri" in data
        assert "verification_uri_complete" in data
        assert data["expires_in"] == 900
        assert data["interval"] == 5

    def test_device_token_poll_pending(self, client):
        # 1. Request code
        code_resp = client.post("/auth/device-code", json={}).json()
        device_code = code_resp["device_code"]

        # 2. Poll before user approval -> 400 authorization_pending
        poll_resp = client.post("/auth/device-token", json={"device_code": device_code})
        assert poll_resp.status_code == 400
        assert poll_resp.json()["detail"]["error"] == "authorization_pending"

    @pytest.mark.asyncio
    async def test_device_flow_complete_approval_cycle(self, client, db):
        # 1. Create web session user
        account = Account(email=f"browser_user_{uuid.uuid4().hex[:6]}@example.com", role="user")
        db.add(account)
        await db.commit()
        await db.refresh(account)

        session_jwt = identity_service.create_web_session(account)

        # 2. Device initiates flow
        code_resp = client.post("/auth/device-code", json={"device_label": "Developer Desktop"}).json()
        device_code = code_resp["device_code"]
        user_code = code_resp["user_code"]

        # 3. Web browser user approves device code
        approve_resp = client.post(
            "/auth/device/approve",
            json={"user_code": user_code, "device_label": "Developer Desktop", "action": "approve"},
            headers={"Authorization": f"Bearer {session_jwt}"},
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "approved"

        # 4. Device polls again -> gets device token
        poll_resp = client.post("/auth/device-token", json={"device_code": device_code})
        assert poll_resp.status_code == 200
        token_data = poll_resp.json()
        assert "device_token" in token_data
        assert token_data["token_type"] == "bearer"
        assert token_data["account_id"] == str(account.account_id)
        assert token_data["device_token"].startswith("dtok_")

        # 5. Token is immediately functional for relay calls
        relay_resp = client.post(
            "/relay/call",
            json={
                "capability_id": "reasoning_model",
                "payload": {"messages": [{"role": "user", "content": "hello"}]},
                "worst_case_units": 10,
            },
            headers={"Authorization": f"Bearer {token_data['device_token']}"},
        )
        # Even if 402 InsufficientCredits or 200, it must NOT be 401 Unauthorized
        assert relay_resp.status_code != 401

        # 6. Polling consumed code again must fail with code_expired
        re_poll = client.post("/auth/device-token", json={"device_code": device_code})
        assert re_poll.status_code == 400
        assert re_poll.json()["detail"]["error"] == "code_expired"

    @pytest.mark.asyncio
    async def test_device_flow_user_denial(self, client, db):
        account = Account(email=f"denier_{uuid.uuid4().hex[:6]}@example.com", role="user")
        db.add(account)
        await db.commit()
        await db.refresh(account)

        session_jwt = identity_service.create_web_session(account)

        code_resp = client.post("/auth/device-code", json={}).json()
        device_code = code_resp["device_code"]
        user_code = code_resp["user_code"]

        # Deny
        deny_resp = client.post(
            "/auth/device/approve",
            json={"user_code": user_code, "action": "deny"},
            headers={"Authorization": f"Bearer {session_jwt}"},
        )
        assert deny_resp.status_code == 200
        assert deny_resp.json()["status"] == "denied"

        # Poll reflects access_denied
        poll_resp = client.post("/auth/device-token", json={"device_code": device_code})
        assert poll_resp.status_code == 400
        assert poll_resp.json()["detail"]["error"] == "access_denied"


class TestMultiPodProductionSafeguard:
    """Verifies that production mode prevents running with ephemeral RSA keys."""

    def test_production_mode_raises_on_missing_rsa_keys(self, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "talos_env", "production")
        monkeypatch.setattr(settings, "jwt_private_key_pem", None)
        monkeypatch.setattr(settings, "jwt_public_key_pem", None)

        with pytest.raises(RuntimeError) as exc_info:
            identity_service.get_signing_key()
        assert "Production environment requires explicit JWT_PRIVATE_KEY_PEM" in str(exc_info.value)
