"""
Talos Cloud — Production Hardening Tests for Authentication Security (Tasks 1-4).

Covers:
  Task 1: Removal of all hardcoded authentication backdoors.
  Task 2: Hardened RS256 Web Session JWT architecture, claims, and key rotation.
  Task 3: Persistent Web Session revocation, refresh token rotation, replay detection, logout, logout-all, sessions list, and delete.
  Task 4: Google OAuth/OIDC account-linking security, unverified email rejection, and hijack prevention.
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models.accounts  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.ledger  # noqa: F401
import app.models.margin_snapshot  # noqa: F401
import app.models.pricing  # noqa: F401
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.accounts import Account, DeviceToken, WebSessionRecord
from app.services import auth_service, identity_service
from app.services.identity_service import AccountLinkingError, InvalidSessionError, WebSession


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


# ═════════════════════════════════════════════════════════════════════════════
# TASK 1: Remove all hardcoded authentication backdoors
# ═════════════════════════════════════════════════════════════════════════════

class TestTask1BackdoorRemoval:
    MAGIC_TOKENS = ["test-token-123", "dev-token", "talos-dev-token", "talos-smoke-test-key"]

    @pytest.mark.parametrize("token", MAGIC_TOKENS)
    def test_relay_rejects_magic_backdoor_tokens(self, client, token):
        """Relay endpoint must reject all backdoor strings with 401 Unauthorized."""
        resp = client.post(
            "/relay/call",
            json={
                "capability_id": "reasoning_model",
                "payload": {"messages": []},
                "worst_case_units": 100,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401
        assert "Invalid or expired device token" in resp.json()["detail"]

    @pytest.mark.parametrize("token", MAGIC_TOKENS)
    def test_auth_session_me_rejects_magic_backdoor_tokens(self, client, token):
        """Web session /session/me endpoint must reject all backdoor strings with 401."""
        resp = client.get(
            "/auth/session/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    @pytest.mark.parametrize("token", MAGIC_TOKENS)
    def test_device_register_rejects_magic_backdoor_tokens(self, client, token):
        """Device registration must reject all backdoor strings with 401."""
        resp = client.post(
            "/auth/device/register",
            json={"device_label": "Hacker Laptop"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_magic_token_does_not_auto_create_superadmin(self, client, db):
        """Presenting a magic token must NOT auto-create shikharjadav16@gmail.com with 1B credits."""
        client.post(
            "/relay/call",
            json={"capability_id": "reasoning_model", "payload": {}, "worst_case_units": 10},
            headers={"Authorization": "Bearer test-token-123"},
        )
        res = await db.execute(
            select(Account).where(Account.email == "shikharjadav16@gmail.com")
        )
        auto_admin = res.scalar_one_or_none()
        assert auto_admin is None or auto_admin.balance_credits < 1_000_000_000


# ═════════════════════════════════════════════════════════════════════════════
# TASK 2: Harden Web Session JWT Architecture
# ═════════════════════════════════════════════════════════════════════════════

class TestTask2WebSessionJWT:
    @pytest.mark.asyncio
    async def test_web_session_jwt_uses_rs256_and_strict_claims(self, db):
        """Web session JWT must use RS256 algorithm and contain kid, iss, aud, sub, sid."""
        acc = Account(email=f"jwt_test_{uuid.uuid4().hex[:6]}@example.com", role="user")
        db.add(acc)
        await db.commit()
        await db.refresh(acc)

        get_settings.cache_clear()
        token = identity_service.create_web_session(acc)

        # 1. Header validation
        header = jwt.get_unverified_header(token)
        assert header["alg"] == "RS256"
        assert "kid" in header
        assert header["typ"] == "JWT"

        # 2. Claims validation
        claims = jwt.get_unverified_claims(token)
        assert claims["sub"] == str(acc.account_id)
        assert claims["email"] == acc.email
        assert claims["role"] == "user"
        assert claims["type"] == "web_session"
        assert claims["iss"] == "talos-cloud"
        assert claims["aud"] == "talos-web"
        assert "iat" in claims
        assert "exp" in claims
        assert "jti" in claims

        # Expiry must be short-lived (15 minutes default)
        diff_minutes = (claims["exp"] - claims["iat"]) / 60
        assert diff_minutes <= 15.1

        # 3. Successful verification
        session = identity_service.verify_web_session(token)
        assert session.account_id == acc.account_id
        assert session.email == acc.email

    @pytest.mark.asyncio
    async def test_tampered_token_rejected(self, db):
        """Tampered RS256 token must fail signature verification."""
        acc = Account(email=f"tamper_{uuid.uuid4().hex[:6]}@example.com", role="user")
        db.add(acc)
        await db.commit()

        token = identity_service.create_web_session(acc)
        # Tamper payload
        parts = token.split(".")
        tampered_token = f"{parts[0]}.{parts[1]}xyz.{parts[2]}"
        with pytest.raises(InvalidSessionError):
            identity_service.verify_web_session(tampered_token)

    @pytest.mark.asyncio
    async def test_key_rotation_with_keyring(self, db):
        """Tokens signed with a previous private key verify when public key is in keyring."""
        # Generate previous RSA keypair
        prev_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        prev_priv_pem = prev_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        prev_pub_pem = prev_private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        acc = Account(email=f"rotate_{uuid.uuid4().hex[:6]}@example.com", role="user")
        db.add(acc)
        await db.commit()

        # Sign token using the old key with kid="talos-old-v0"
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(acc.account_id),
            "email": acc.email,
            "role": acc.role,
            "type": "web_session",
            "iss": "talos-cloud",
            "aud": "talos-web",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=15)).timestamp()),
        }
        old_token = jwt.encode(
            payload, prev_priv_pem, algorithm="RS256", headers={"alg": "RS256", "kid": "talos-old-v0"}
        )

        # Before adding to keyring: must be rejected with unknown kid
        with pytest.raises(InvalidSessionError, match="Unknown signing key ID"):
            identity_service.verify_web_session(old_token)

        # Add to keyring in settings
        settings = get_settings()
        old_keyring = settings.jwt_previous_public_keys
        settings.jwt_previous_public_keys = json.dumps({"talos-old-v0": prev_pub_pem})
        try:
            # Now verification must succeed!
            session = identity_service.verify_web_session(old_token)
            assert session.account_id == acc.account_id
        finally:
            settings.jwt_previous_public_keys = old_keyring

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, db):
        """Expired RS256 token must raise InvalidSessionError."""
        acc = Account(email=f"exp_{uuid.uuid4().hex[:6]}@example.com", role="user")
        db.add(acc)
        await db.commit()

        # Issue token with negative expiry
        expired_token = identity_service.create_web_session(acc, expiry_minutes=-5)
        with pytest.raises(InvalidSessionError, match="Signature has expired|Invalid session token"):
            identity_service.verify_web_session(expired_token)

    @pytest.mark.asyncio
    async def test_token_isolation_enforced(self, db, client):
        """Device tokens cannot access /auth/session/me, and Web Sessions cannot access /relay/call."""
        acc = Account(email=f"iso_{uuid.uuid4().hex[:6]}@example.com", balance_credits=500)
        db.add(acc)
        await db.commit()

        # 1. Device Token
        raw_dev_token = auth_service._generate_raw_token()
        dt = DeviceToken(
            account_id=acc.account_id,
            token_hash=auth_service._hash_token(raw_dev_token),
            expires_at=auth_service._expiry(),
        )
        db.add(dt)
        await db.commit()

        # Calling web session endpoint with device token fails with 401
        resp = client.get("/auth/session/me", headers={"Authorization": f"Bearer {raw_dev_token}"})
        assert resp.status_code == 401

        # 2. Web Session token
        web_token = identity_service.create_web_session(acc)
        # Calling relay with web session token fails with 401
        resp2 = client.post(
            "/relay/call",
            json={"capability_id": "reasoning_model", "payload": {}, "worst_case_units": 10},
            headers={"Authorization": f"Bearer {web_token}"},
        )
        assert resp2.status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
# TASK 3: Persistent Web Session Revocation & Refresh Management
# ═════════════════════════════════════════════════════════════════════════════

class TestTask3WebSessionRevocation:
    @pytest.mark.asyncio
    async def test_create_session_and_refresh_rotation(self, db):
        """Session creation stores hashed refresh token; rotation replaces secret and detects replays."""
        acc = Account(email=f"sess_{uuid.uuid4().hex[:6]}@example.com", role="user")
        db.add(acc)
        await db.commit()

        access_token, raw_refresh, record = await identity_service.create_web_session_record(
            db=db, account=acc, user_agent="Mozilla/5.0 TestBrowser", ip_address="127.0.0.1"
        )
        await db.commit()

        assert record.session_id is not None
        assert record.refresh_token_hash != raw_refresh
        assert not record.is_revoked
        initial_hash = record.refresh_token_hash

        # Verify access token has sid claim
        session = identity_service.verify_web_session(access_token)
        assert session.session_id == record.session_id

        # 1. Rotate refresh token
        new_access, new_refresh, updated_rec = await identity_service.rotate_refresh_token(
            db=db, raw_refresh_token=raw_refresh
        )
        await db.commit()

        assert new_refresh != raw_refresh
        assert updated_rec.refresh_token_hash != initial_hash

        # 2. Replay attack detection: Attempting to reuse old raw_refresh MUST fail and revoke session!
        with pytest.raises(InvalidSessionError, match="Session has been revoked for security|Invalid refresh token"):
            await identity_service.rotate_refresh_token(db=db, raw_refresh_token=raw_refresh)
        await db.commit()

        # Check that session is now marked revoked in DB
        await db.refresh(updated_rec)
        assert updated_rec.is_revoked

        # 3. Trying new_refresh on a revoked session MUST fail
        with pytest.raises(InvalidSessionError, match="Web session has been revoked"):
            await identity_service.rotate_refresh_token(db=db, raw_refresh_token=new_refresh)

    @pytest.mark.asyncio
    async def test_logout_revokes_current_session(self, db, client):
        """POST /auth/logout revokes session; subsequent requests fail with 401."""
        acc = Account(email=f"logout_{uuid.uuid4().hex[:6]}@example.com", role="user")
        db.add(acc)
        await db.commit()

        access_token, raw_refresh, record = await identity_service.create_web_session_record(
            db=db, account=acc
        )
        await db.commit()

        # Authenticated call succeeds
        r1 = client.get("/auth/session/me", headers={"Authorization": f"Bearer {access_token}"})
        assert r1.status_code == 200

        # Logout
        r_logout = client.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert r_logout.status_code == 200
        assert r_logout.json()["revoked"] is True

        # Subsequent call with revoked token fails with 401
        r2 = client.get("/auth/session/me", headers={"Authorization": f"Bearer {access_token}"})
        assert r2.status_code == 401
        assert "Session has been revoked" in r2.json()["detail"]

    @pytest.mark.asyncio
    async def test_logout_all_revokes_all_active_sessions(self, db, client):
        """POST /auth/logout-all revokes all sessions across all devices for this account."""
        acc = Account(email=f"logout_all_{uuid.uuid4().hex[:6]}@example.com", role="user")
        db.add(acc)
        await db.commit()

        # Create 3 sessions
        tok1, _, _ = await identity_service.create_web_session_record(db=db, account=acc)
        tok2, _, _ = await identity_service.create_web_session_record(db=db, account=acc)
        tok3, _, _ = await identity_service.create_web_session_record(db=db, account=acc)
        await db.commit()

        # Check session listing before
        r_list = client.get("/auth/sessions", headers={"Authorization": f"Bearer {tok1}"})
        assert r_list.status_code == 200
        assert len(r_list.json()) == 3

        # Trigger logout-all
        r_logout_all = client.post("/auth/logout-all", headers={"Authorization": f"Bearer {tok1}"})
        assert r_logout_all.status_code == 200
        assert r_logout_all.json()["revoked_count"] == 3

        # All 3 tokens are now revoked
        assert client.get("/auth/session/me", headers={"Authorization": f"Bearer {tok1}"}).status_code == 401
        assert client.get("/auth/session/me", headers={"Authorization": f"Bearer {tok2}"}).status_code == 401
        assert client.get("/auth/session/me", headers={"Authorization": f"Bearer {tok3}"}).status_code == 401

    @pytest.mark.asyncio
    async def test_session_listing_and_delete(self, db, client):
        """GET /auth/sessions and DELETE /auth/sessions/{id} allow managing individual sessions."""
        acc = Account(email=f"manage_{uuid.uuid4().hex[:6]}@example.com", role="user")
        db.add(acc)
        await db.commit()

        tok_main, _, rec_main = await identity_service.create_web_session_record(
            db=db, account=acc, user_agent="Desktop Firefox"
        )
        tok_other, _, rec_other = await identity_service.create_web_session_record(
            db=db, account=acc, user_agent="Mobile Safari"
        )
        await db.commit()

        # List sessions
        r_list = client.get("/auth/sessions", headers={"Authorization": f"Bearer {tok_main}"})
        assert r_list.status_code == 200
        sessions = r_list.json()
        assert len(sessions) == 2

        # Check is_current flag
        current_item = next(s for s in sessions if s["session_id"] == str(rec_main.session_id))
        assert current_item["is_current"] is True
        other_item = next(s for s in sessions if s["session_id"] == str(rec_other.session_id))
        assert other_item["is_current"] is False

        # Delete the other session
        r_del = client.delete(
            f"/auth/sessions/{rec_other.session_id}",
            headers={"Authorization": f"Bearer {tok_main}"},
        )
        assert r_del.status_code == 200

        # Other token is now revoked
        assert client.get("/auth/session/me", headers={"Authorization": f"Bearer {tok_other}"}).status_code == 401

        # Main token is still valid
        assert client.get("/auth/session/me", headers={"Authorization": f"Bearer {tok_main}"}).status_code == 200


# ═════════════════════════════════════════════════════════════════════════════
# TASK 4: Google OAuth/OIDC Account-Linking Security
# ═════════════════════════════════════════════════════════════════════════════

class TestTask4GoogleOAuthHardening:
    @pytest.mark.asyncio
    async def test_unverified_google_email_rejected_in_resolution(self, db):
        """resolve_google_account must REJECT unverified Google emails."""
        unique_email = f"unverified_{uuid.uuid4().hex[:6]}@victim.com"
        google_sub = f"hacker_sub_{uuid.uuid4().hex[:6]}"

        with pytest.raises(AccountLinkingError, match="Google email .* is not verified"):
            await identity_service.resolve_google_account(
                db=db,
                google_sub=google_sub,
                email=unique_email,
                email_verified=False,
            )

    def test_unverified_google_email_rejected_in_callback(self, client):
        """GET /auth/google/callback must reject unverified email with 400."""
        code = f"unverified_code_{uuid.uuid4().hex[:6]}"
        mock_profile = {
            "sub": f"google-sub-{code}",
            "email": "victim@example.com",
            "email_verified": False,
            "name": "Attacker",
        }
        with patch("app.services.identity_service.exchange_google_code", return_value=mock_profile):
            resp = client.get(f"/auth/google/callback?code={code}")
            assert resp.status_code == 400
            assert "email is not verified" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_unverified_email_cannot_hijack_existing_account(self, db):
        """An unverified Google identity matching an existing account's email must NEVER link."""
        victim_email = f"victim_{uuid.uuid4().hex[:6]}@example.com"
        acc = Account(email=victim_email, google_sub=None, role="user", balance_credits=1000)
        db.add(acc)
        await db.commit()

        # Attacker tries to log in with victim's email unverified
        with pytest.raises(AccountLinkingError, match="Google email .* is not verified"):
            await identity_service.resolve_google_account(
                db=db,
                google_sub="attacker_google_sub_123",
                email=victim_email,
                email_verified=False,
            )

        # Account must remain untouched and unlinked
        await db.refresh(acc)
        assert acc.google_sub is None

    @pytest.mark.asyncio
    async def test_verified_google_email_safely_links_existing_subless_account(self, db):
        """Existing account with google_sub=None links safely when email is verified."""
        email = f"verified_link_{uuid.uuid4().hex[:6]}@example.com"
        acc = Account(email=email, google_sub=None, role="user", balance_credits=50)
        db.add(acc)
        await db.commit()

        google_sub = f"google_sub_{uuid.uuid4().hex[:6]}"
        linked_acc = await identity_service.resolve_google_account(
            db=db,
            google_sub=google_sub,
            email=email,
            email_verified=True,
        )
        await db.commit()

        assert linked_acc.account_id == acc.account_id
        assert linked_acc.google_sub == google_sub

    @pytest.mark.asyncio
    async def test_sub_conflict_rejected(self, db):
        """Account already linked to sub_A rejects linking to sub_B even with verified email."""
        email = f"conflict_{uuid.uuid4().hex[:6]}@example.com"
        acc = Account(email=email, google_sub="sub_legit_owner", role="user")
        db.add(acc)
        await db.commit()

        with pytest.raises(AccountLinkingError, match="already linked to a different Google identity"):
            await identity_service.resolve_google_account(
                db=db,
                google_sub="sub_different_account",
                email=email,
                email_verified=True,
            )
