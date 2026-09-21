"""
Tests for Phase 0 + Phase 1: DB setup, auth service, token contract.
Uses in-memory SQLite for test isolation (auth logic; not relay atomicity tests).
"""

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Use SQLite for auth unit tests — we're testing auth logic, not Postgres semantics.
# The relay atomicity tests (test_relay.py) use a real Postgres instance.
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


import os


@pytest_asyncio.fixture(scope="session")
async def engine_fixture():
    from app.database import Base as AppBase
    import app.models.accounts  # noqa: F401
    import app.models.ledger  # noqa: F401
    import app.models.pricing  # noqa: F401
    import app.models.billing  # noqa: F401
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
        await conn.run_sync(AppBase.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db(engine_fixture):
    async_session = async_sessionmaker(engine_fixture, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session


class TestAuthService:
    """Phase 1: Auth service correctness tests."""

    @pytest.mark.asyncio
    async def test_register_creates_account_and_issues_token(self, db):
        from app.services.auth_service import register_account, authenticate_token
        account, raw_token = await register_account(db, email=f"test_{uuid.uuid4().hex[:8]}@example.com")
        assert account.account_id is not None
        assert raw_token
        assert len(raw_token) > 30

        # Token must be valid immediately after registration
        dt = await authenticate_token(db, raw_token)
        assert dt is not None
        assert dt.account_id == account.account_id

    @pytest.mark.asyncio
    async def test_token_hash_not_equal_to_raw(self, db):
        """Server stores hash, not raw token. Hash != raw token."""
        from app.services.auth_service import register_account
        _, raw_token = await register_account(db, email=f"hash_{uuid.uuid4().hex[:8]}@example.com")

        # Fetch the DeviceToken row directly
        from sqlalchemy import select
        from app.models.accounts import DeviceToken
        result = await db.execute(select(DeviceToken).order_by(DeviceToken.created_at.desc()).limit(1))
        dt = result.scalar_one()
        # Stored hash must differ from raw token
        assert dt.token_hash != raw_token
        # Hash must look like a bcrypt hash
        assert dt.token_hash.startswith("$2b$") or dt.token_hash.startswith("$2a$")

    @pytest.mark.asyncio
    async def test_refresh_invalidates_old_token(self, db):
        """After refresh, old token must be invalid."""
        from app.services.auth_service import register_account, refresh_token, authenticate_token
        _, old_raw = await register_account(db, email=f"refresh_{uuid.uuid4().hex[:8]}@example.com")

        result = await refresh_token(db, raw_old_token=old_raw)
        assert result is not None
        _, new_raw = result

        # Old token is now revoked
        old_dt = await authenticate_token(db, old_raw)
        assert old_dt is None

        # New token is valid
        new_dt = await authenticate_token(db, new_raw)
        assert new_dt is not None

    @pytest.mark.asyncio
    async def test_revoke_token(self, db):
        """Revoked token must not authenticate."""
        from app.services.auth_service import register_account, revoke_token, authenticate_token
        _, raw = await register_account(db, email=f"revoke_{uuid.uuid4().hex[:8]}@example.com")

        ok = await revoke_token(db, raw_token=raw)
        assert ok is True

        dt = await authenticate_token(db, raw)
        assert dt is None

    @pytest.mark.asyncio
    async def test_invalid_token_returns_none(self, db):
        """Random strings must not authenticate."""
        from app.services.auth_service import authenticate_token
        dt = await authenticate_token(db, "not-a-real-token-abc123")
        assert dt is None

    @pytest.mark.asyncio
    async def test_raw_token_not_re_derivable(self, db):
        """
        Verify contract: server cannot re-derive raw token from stored hash.
        This test documents the invariant: once the registration response is sent,
        the raw token is gone from the server's perspective.
        """
        from app.services.auth_service import register_account, _hash_token
        _, raw = await register_account(db, email=f"noderiv_{uuid.uuid4().hex[:8]}@example.com")

        # Hash the same raw token again — it produces a DIFFERENT hash each time (bcrypt salt)
        hash1 = _hash_token(raw)
        hash2 = _hash_token(raw)
        assert hash1 != hash2, "bcrypt must produce different hashes for same input (salt)"
        # This is the property that makes the hash non-reversible by the server
