"""
Tests for Marketplace Unpublish Permissions:
  1. Admin can unpublish ANY listing from the marketplace.
  2. Normal user can unpublish THEIR OWN published listing.
  3. Normal user CANNOT unpublish someone else's listing (HTTP 403 Forbidden).
  4. Unauthenticated request cannot unpublish (HTTP 401 Unauthorized).
"""

import os
import uuid
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models.accounts  # noqa: F401
import app.models.marketplace  # noqa: F401
from app.database import Base, get_db
from app.main import app
from app.models.accounts import Account
from app.models.marketplace import MarketplaceListing
from app.services import identity_service


@pytest_asyncio.fixture(scope="module")
async def test_engine():
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


@pytest.mark.asyncio
async def test_marketplace_unpublish_permissions(db_session, client):
    # 1. Create Admin Account
    admin = Account(
        account_id=uuid.uuid4(),
        email="admin_user@talos.ai",
        role="admin",
        subscription_tier="admin",
    )
    db_session.add(admin)

    # 2. Create User Alice (Normal user)
    alice = Account(
        account_id=uuid.uuid4(),
        email="alice@example.com",
        role="user",
        subscription_tier="free",
    )
    db_session.add(alice)

    # 3. Create User Bob (Normal user)
    bob = Account(
        account_id=uuid.uuid4(),
        email="bob@example.com",
        role="user",
        subscription_tier="free",
    )
    db_session.add(bob)

    # 4. Create Listings:
    # - alice_mcp (published by Alice)
    # - bob_agent (published by Bob)
    listing_alice = MarketplaceListing(
        listing_id=uuid.uuid4(),
        author_account_id=alice.account_id,
        author_username="alice",
        kind="mcp",
        slug="alice-gmail-connector",
        display_name="Alice Gmail Connector",
        tagline="Custom Gmail MCP",
        icon_emoji="📧",
        icon_color="#ea4335",
        tags=["mcp", "gmail"],
        manifest_yaml="name: alice-gmail-connector\nkind: mcp\n",
        status="approved",
        version="1.0.0",
        is_builtin=False,
    )
    db_session.add(listing_alice)

    listing_bob = MarketplaceListing(
        listing_id=uuid.uuid4(),
        author_account_id=bob.account_id,
        author_username="bob",
        kind="agent",
        slug="bob-researcher",
        display_name="Bob Research Agent",
        tagline="Agent for research",
        icon_emoji="🤖",
        icon_color="#3b82f6",
        tags=["agent"],
        manifest_yaml="name: bob-researcher\nkind: agent\n",
        status="approved",
        version="1.0.0",
        is_builtin=False,
    )
    db_session.add(listing_bob)
    await db_session.commit()

    # Generate JWT tokens
    admin_token = identity_service.create_web_session(admin)
    alice_token = identity_service.create_web_session(alice)
    bob_token = identity_service.create_web_session(bob)

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    alice_headers = {"Authorization": f"Bearer {alice_token}"}
    bob_headers = {"Authorization": f"Bearer {bob_token}"}

    # Case 1: Unauthenticated request cannot unpublish (HTTP 401)
    resp_unauth = client.delete("/marketplace/listings/alice/alice-gmail-connector")
    assert resp_unauth.status_code == 401, f"Expected 401, got {resp_unauth.status_code}: {resp_unauth.text}"

    # Case 2: Normal user Bob CANNOT unpublish Alice's listing (HTTP 403 Forbidden)
    resp_bob_on_alice = client.delete(
        "/marketplace/listings/alice/alice-gmail-connector",
        headers=bob_headers,
    )
    assert resp_bob_on_alice.status_code == 403, f"Expected 403, got {resp_bob_on_alice.status_code}: {resp_bob_on_alice.text}"
    assert "Forbidden" in resp_bob_on_alice.json()["detail"]

    # Case 3: Normal user Alice CAN unpublish her OWN listing (HTTP 200 OK)
    resp_alice_own = client.delete(
        "/marketplace/listings/alice/alice-gmail-connector",
        headers=alice_headers,
    )
    assert resp_alice_own.status_code == 200, f"Expected 200, got {resp_alice_own.status_code}: {resp_alice_own.text}"
    assert resp_alice_own.json() == {"ok": True}

    # Verify Alice's listing is now gone from cloud
    resp_get_alice = client.get("/marketplace/listings/alice/alice-gmail-connector")
    assert resp_get_alice.status_code == 404

    # Case 4: Admin CAN unpublish Bob's listing even though admin is NOT the author (HTTP 200 OK)
    resp_admin_on_bob = client.delete(
        "/marketplace/listings/bob/bob-researcher",
        headers=admin_headers,
    )
    assert resp_admin_on_bob.status_code == 200, f"Expected 200, got {resp_admin_on_bob.status_code}: {resp_admin_on_bob.text}"
    assert resp_admin_on_bob.json() == {"ok": True}

    # Verify Bob's listing is now gone from cloud
    resp_get_bob = client.get("/marketplace/listings/bob/bob-researcher")
    assert resp_get_bob.status_code == 404
