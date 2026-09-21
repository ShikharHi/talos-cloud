"""
Talos Cloud — Phase 10a Identity & Google OAuth Tests.

Tests:
  1. New Google user creates account with google_sub, email, role='user' and default credits.
  2. Returning Google user retrieves existing account without duplicate creation.
  3. Explicit account-linking policy:
     - Safe linking: existing email-only account with google_sub=None links safely.
     - Hijack protection: existing account with different google_sub rejects with AccountLinkingError.
  4. Web session token cannot be used to call /relay/* (returns 401).
  5. Device registration (POST /auth/device/register) fails without web session (401) and succeeds with web session.
"""

import os
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
import app.models.margin_snapshot  # noqa: F401
import app.models.pricing  # noqa: F401
from app.database import Base, get_db
from app.main import app
from app.models.accounts import Account
from app.services import identity_service
from app.services.identity_service import AccountLinkingError, WebSession


@pytest_asyncio.fixture(scope="module")
async def test_engine():
    from sqlalchemy.pool import NullPool, StaticPool
    db_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    if db_url.startswith("sqlite"):
        engine = create_async_engine(
            db_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False,
        )
    else:
        engine = create_async_engine(db_url, poolclass=NullPool, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine):
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


class TestIdentityService:
    def test_google_auth_url_uses_select_account_prompt(self):
        """Google auth should force the picker instead of silently auto-selecting the default account."""
        url = identity_service.get_google_auth_url(state="http://localhost:3000")

        assert "https://accounts.google.com/o/oauth2/v2/auth" in url
        assert "prompt=select_account" in url
        assert "AccountChooser" not in url

    @pytest.mark.asyncio
    async def test_google_login_new_user_creates_account(self, db_session):
        """New Google user creates account with correct attributes and welcome credits."""
        unique_sub = f"sub_new_{uuid.uuid4().hex[:8]}"
        unique_email = f"user_{uuid.uuid4().hex[:8]}@example.com"

        account = await identity_service.resolve_google_account(
            db=db_session,
            google_sub=unique_sub,
            email=unique_email,
            email_verified=True,
            default_free_credits=10,
        )
        await db_session.commit()

        assert account.email == unique_email
        assert account.google_sub == unique_sub
        assert account.role == "user"
        assert account.balance_credits == 10

        # Create web session token
        token = identity_service.create_web_session(account)
        session = identity_service.verify_web_session(token)
        assert session.account_id == account.account_id
        assert session.email == unique_email
        assert session.role == "user"
        assert session.google_sub == unique_sub

    @pytest.mark.asyncio
    async def test_google_login_returning_user_resolves_existing(self, db_session):
        """Returning Google user gets existing account without creating duplicate."""
        unique_sub = f"sub_ret_{uuid.uuid4().hex[:8]}"
        unique_email = f"ret_{uuid.uuid4().hex[:8]}@example.com"

        acc1 = await identity_service.resolve_google_account(
            db=db_session,
            google_sub=unique_sub,
            email=unique_email,
            email_verified=True,
        )
        await db_session.commit()

        acc2 = await identity_service.resolve_google_account(
            db=db_session,
            google_sub=unique_sub,
            email=unique_email,
            email_verified=True,
        )
        assert acc1.account_id == acc2.account_id

        # Verify exactly 1 account in DB
        res = await db_session.execute(select(Account).where(Account.google_sub == unique_sub))
        assert len(res.scalars().all()) == 1

    @pytest.mark.asyncio
    async def test_explicit_account_linking_policy(self, db_session):
        """Explicit account linking policy: links if google_sub is None, rejects conflict."""
        email = f"link_test_{uuid.uuid4().hex[:8]}@example.com"
        sub_a = f"sub_a_{uuid.uuid4().hex[:8]}"
        sub_b = f"sub_b_{uuid.uuid4().hex[:8]}"

        # Existing account with no google_sub
        acc = Account(email=email, google_sub=None, role="user", balance_credits=500)
        db_session.add(acc)
        await db_session.commit()
        await db_session.refresh(acc)

        # 1. Safe link with verified Google email
        linked_acc = await identity_service.resolve_google_account(
            db=db_session,
            google_sub=sub_a,
            email=email,
            email_verified=True,
        )
        await db_session.commit()
        assert linked_acc.account_id == acc.account_id
        assert linked_acc.google_sub == sub_a

        # 2. Conflict attempt with different google_sub for same email -> rejected
        with pytest.raises(AccountLinkingError):
            await identity_service.resolve_google_account(
                db=db_session,
                google_sub=sub_b,
                email=email,
                email_verified=True,
            )

    @pytest.mark.asyncio
    async def test_admin_email_configured_as_admin(self, db_session):
        """Admin email (shikharjadav16@gmail.com) gets role='admin' and unlimited balance."""
        admin_email = "shikharjadav16@gmail.com"
        admin_sub = f"sub_admin_{uuid.uuid4().hex[:6]}"

        acc = await identity_service.resolve_google_account(
            db=db_session,
            google_sub=admin_sub,
            email=admin_email,
            email_verified=True,
        )
        await db_session.commit()

        assert acc.email == admin_email
        assert acc.role == "admin"
        assert acc.subscription_tier == "admin"
        assert acc.balance_credits >= 1_000_000_000



class TestTokenIsolationAndDeviceAuth:
    def test_web_session_cannot_call_relay(self, client):
        """Web session JWT is rejected on /relay/* endpoints with 401 Unauthorized."""
        mock_acc = Account(
            account_id=uuid.uuid4(),
            email="webuser@example.com",
            role="user",
            balance_credits=1000,
        )
        web_token = identity_service.create_web_session(mock_acc)

        resp = client.post(
            "/relay/call",
            json={
                "capability_id": "reasoning_model",
                "payload": {"messages": []},
                "worst_case_units": 1000,
            },
            headers={"Authorization": f"Bearer {web_token}"},
        )
        assert resp.status_code == 401

    def test_device_registration_fails_without_web_session(self, client):
        """POST /auth/device/register fails with 401 if no web session is provided."""
        resp = client.post("/auth/device/register", json={"device_label": "My Laptop"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_device_registration_succeeds_with_web_session(self, db_session, client):
        """POST /auth/device/register succeeds when authenticated with a web session."""
        mock_acc = Account(
            account_id=uuid.uuid4(),
            email=f"devuser_{uuid.uuid4().hex[:6]}@example.com",
            role="user",
            balance_credits=1000,
        )
        db_session.add(mock_acc)
        await db_session.commit()

        web_token = identity_service.create_web_session(mock_acc)

        resp = client.post(
            "/auth/device/register",
            json={"device_label": "Workstation"},
            headers={"Authorization": f"Bearer {web_token}"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["account_id"] == str(mock_acc.account_id)
        assert "raw_token" in data
        assert len(data["raw_token"]) > 30

    def test_google_callback_endpoint(self, client):
        """GET /auth/google/callback exchanges code and redirects to UI."""
        from unittest.mock import patch
        code = f"test_code_{uuid.uuid4().hex[:6]}"
        mock_profile = {
            "sub": f"google-sub-{code}",
            "email": f"{code}@gmail.com",
            "email_verified": True,
            "name": f"User {code}",
        }
        with patch("app.services.identity_service.exchange_google_code", return_value=mock_profile):
            resp = client.get(f"/auth/google/callback?code={code}", follow_redirects=False)
            assert resp.status_code == 302
