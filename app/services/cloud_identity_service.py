"""
Talos Cloud — Unified Cloud Identity Service.

Handles:
  1. Multi-provider Identity federation (Google, GitHub, Microsoft, OIDC).
  2. Device registration, heartbeat, and revocation (Windows, macOS, Linux).
  3. Session lifecycle (Web, Desktop, CLI, Remote, Service) with rotating refresh tokens.
  4. Scoped API Key management (generation, SHA-256 storage, verification).
  5. Short-lived Access Token minting (RS256 JWTs with sub, did, sid, scopes).
  6. Project & Organization management.
  7. High-speed Redis revocation and session caching with Postgres authority.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from jose import JWTError, jwt
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.accounts import (
    Account,
    ApiKey,
    Device,
    Identity,
    Organization,
    OrganizationMember,
    Project,
    ProjectMember,
    Session,
)
from app.services import identity_service
from app.services.rate_limiter import get_redis_client

logger = logging.getLogger("talos.cloud.identity")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(secret: str) -> str:
    """Computes SHA-256 digest of secret token."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ─── Redis Acceleration Helpers ───────────────────────────────────────────────

async def cache_session(session_id: uuid.UUID, account_id: uuid.UUID, device_id: uuid.UUID | None, ttl_seconds: int = 900) -> None:
    try:
        redis = await get_redis_client()
        if redis:
            val = f"{account_id.hex}:{device_id.hex if device_id else ''}"
            await redis.set(f"auth:session:{session_id.hex}", val, ex=ttl_seconds)
    except Exception as e:
        logger.debug("Redis cache_session error: %s", e)


async def invalidate_session_cache(session_id: uuid.UUID) -> None:
    try:
        redis = await get_redis_client()
        if redis:
            await redis.delete(f"auth:session:{session_id.hex}")
            await redis.set(f"auth:revoked_session:{session_id.hex}", "1", ex=86400)
    except Exception as e:
        logger.debug("Redis invalidate_session_cache error: %s", e)


async def mark_device_revoked_cache(device_id: uuid.UUID) -> None:
    try:
        redis = await get_redis_client()
        if redis:
            await redis.set(f"auth:revoked_device:{device_id.hex}", "1", ex=86400)
    except Exception as e:
        logger.debug("Redis mark_device_revoked_cache error: %s", e)


async def is_device_revoked_cache(device_id: uuid.UUID) -> bool:
    try:
        redis = await get_redis_client()
        if redis:
            val = await redis.get(f"auth:revoked_device:{device_id.hex}")
            return val == "1"
    except Exception:
        pass
    return False


# ─── Device Management ────────────────────────────────────────────────────────

async def register_device(
    db: AsyncSession,
    account_id: uuid.UUID,
    device_name: str,
    platform: str,
    device_type: str = "desktop",
    os_version: str | None = None,
    app_version: str | None = None,
    metadata_json: str | None = None,
) -> Device:
    """Registers a first-class client environment for an account."""
    device = Device(
        device_id=uuid.uuid4(),
        account_id=account_id,
        device_name=device_name or "Talos Client",
        platform=platform or "unknown",
        device_type=device_type or "desktop",
        os_version=os_version,
        app_version=app_version,
        metadata_json=metadata_json,
        status="active",
        created_at=_utcnow(),
        last_seen_at=_utcnow(),
    )
    db.add(device)
    await db.flush()
    return device


async def get_device(db: AsyncSession, device_id: uuid.UUID) -> Device | None:
    return await db.get(Device, device_id)


async def update_device_heartbeat(
    db: AsyncSession,
    device_id: uuid.UUID,
    app_version: str | None = None,
    os_version: str | None = None,
) -> bool:
    """Updates device last_seen_at and metadata."""
    device = await db.get(Device, device_id)
    if not device or device.is_revoked:
        return False
    device.last_seen_at = _utcnow()
    if app_version:
        device.app_version = app_version
    if os_version:
        device.os_version = os_version
    await db.flush()
    return True


async def revoke_device(db: AsyncSession, device_id: uuid.UUID, account_id: uuid.UUID | None = None) -> bool:
    """Revokes a device and all attached sessions."""
    device = await db.get(Device, device_id)
    if not device:
        return False
    if account_id and device.account_id != account_id:
        return False

    now = _utcnow()
    device.status = "revoked"
    device.revoked_at = now

    # Revoke all attached sessions
    stmt = (
        update(Session)
        .where(Session.device_id == device_id, Session.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await db.execute(stmt)
    await db.flush()

    await mark_device_revoked_cache(device_id)
    return True


async def list_devices(db: AsyncSession, account_id: uuid.UUID) -> list[Device]:
    stmt = select(Device).where(Device.account_id == account_id).order_by(Device.last_seen_at.desc())
    res = await db.execute(stmt)
    return list(res.scalars().all())


# ─── Unified Session Lifecycle & Access Tokens ────────────────────────────────

def mint_access_token(
    account: Account,
    session_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    scopes: list[str] | None = None,
    expiry_minutes: int | None = None,
) -> str:
    """
    Mints a cryptographically signed RS256 Cloud Access Token.
    Claims: sub (account_id), sid (session_id), did (device_id), scopes, iss, aud.
    """
    settings = get_settings()
    now = _utcnow()
    exp_mins = expiry_minutes if expiry_minutes is not None else settings.session_expiry_minutes
    exp = now + timedelta(minutes=exp_mins)
    private_key_pem, key_id = identity_service.get_signing_key()

    payload = {
        "sub": str(account.account_id),
        "email": account.email,
        "role": account.role,
        "type": "access_token",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": str(uuid.uuid4()),
        "scope": scopes or ["agent:run", "workspace:read", "workspace:write", "marketplace:read", "marketplace:publish"],
    }
    if session_id:
        payload["sid"] = str(session_id)
    if device_id:
        payload["did"] = str(device_id)

    headers = {
        "alg": "RS256",
        "typ": "JWT",
        "kid": key_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256", headers=headers)


async def create_cloud_session(
    db: AsyncSession,
    account: Account,
    session_type: str = "web",  # web, desktop, cli, remote, service
    device: Device | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    scopes: list[str] | None = None,
) -> tuple[str, str, Session]:
    """
    Creates an authoritative Cloud Session.
    Returns (access_token, raw_refresh_token, session_row).
    Raw refresh token format: {session_id.hex}.{secret}
    """
    settings = get_settings()
    session_id = uuid.uuid4()
    secret = secrets.token_urlsafe(32)
    refresh_hash = _hash_token(secret)
    now = _utcnow()
    expires_at = now + timedelta(days=settings.refresh_token_expiry_days)

    sess = Session(
        session_id=session_id,
        account_id=account.account_id,
        device_id=device.device_id if device else None,
        session_type=session_type,
        refresh_token_hash=refresh_hash,
        ip_address=ip_address[:100] if ip_address else None,
        user_agent=user_agent[:500] if user_agent else None,
        created_at=now,
        last_seen_at=now,
        expires_at=expires_at,
        revoked_at=None,
    )
    db.add(sess)
    await db.flush()

    raw_refresh = f"{session_id.hex}.{secret}"
    access_token = mint_access_token(
        account=account,
        session_id=session_id,
        device_id=device.device_id if device else None,
        scopes=scopes,
    )

    await cache_session(session_id, account.account_id, device.device_id if device else None)
    return access_token, raw_refresh, sess


async def refresh_cloud_session(
    db: AsyncSession,
    raw_refresh_token: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str, Session]:
    """
    Rotates a session refresh token with replay-attack detection and device validation.
    """
    now = _utcnow()
    parts = raw_refresh_token.strip().split(".", 1)
    if len(parts) != 2:
        raise identity_service.InvalidSessionError("Malformed refresh token format.")

    session_id_raw, secret = parts
    try:
        session_id = uuid.UUID(session_id_raw)
    except ValueError as e:
        raise identity_service.InvalidSessionError("Invalid session_id in refresh token.") from e

    # Use with_for_update to lock session row against concurrent race conditions
    stmt = select(Session).where(Session.session_id == session_id).with_for_update()
    res = await db.execute(stmt)
    sess = res.scalar_one_or_none()
    if not sess:
        raise identity_service.InvalidSessionError("Session record not found.")

    if sess.is_revoked:
        logger.warning("SECURITY ALERT: Attempt to use revoked session %s", session_id)
        raise identity_service.InvalidSessionError("Session has been revoked.")

    if sess.expires_at <= now:
        raise identity_service.InvalidSessionError("Session refresh token has expired.")

    # Check attached device status
    if sess.device_id:
        dev = await db.get(Device, sess.device_id)
        if not dev or dev.is_revoked:
            sess.revoked_at = now
            await db.flush()
            await invalidate_session_cache(session_id)
            raise identity_service.InvalidSessionError("Associated device has been revoked.")

    # Replay / theft detection
    expected_hash = _hash_token(secret)
    if not hmac.compare_digest(expected_hash, sess.refresh_token_hash):
        sess.revoked_at = now
        await db.flush()
        await invalidate_session_cache(session_id)
        logger.warning("SECURITY ALERT: Refresh token theft detected for session %s. Revoking.", session_id)
        raise identity_service.InvalidSessionError("Invalid refresh token. Session has been revoked for security.")

    account = await db.get(Account, sess.account_id)
    if not account or account.status != "active":
        raise identity_service.InvalidSessionError("Account is inactive or suspended.")

    # Rotate secret
    new_secret = secrets.token_urlsafe(32)
    sess.refresh_token_hash = _hash_token(new_secret)
    sess.last_seen_at = now
    if ip_address:
        sess.ip_address = ip_address[:100]
    if user_agent:
        sess.user_agent = user_agent[:500]
    await db.flush()

    new_raw_refresh = f"{session_id.hex}.{new_secret}"
    new_access_token = mint_access_token(
        account=account,
        session_id=session_id,
        device_id=sess.device_id,
    )
    await cache_session(session_id, account.account_id, sess.device_id)
    return new_access_token, new_raw_refresh, sess


async def revoke_cloud_session(db: AsyncSession, session_id: uuid.UUID, account_id: uuid.UUID | None = None) -> bool:
    sess = await db.get(Session, session_id)
    if not sess:
        return False
    if account_id and sess.account_id != account_id:
        return False
    if not sess.revoked_at:
        sess.revoked_at = _utcnow()
        await db.flush()
    await invalidate_session_cache(session_id)
    return True


async def revoke_all_user_sessions(db: AsyncSession, account_id: uuid.UUID, revoke_devices: bool = False) -> int:
    now = _utcnow()
    # Invalidate DB sessions
    stmt = (
        update(Session)
        .where(Session.account_id == account_id, Session.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    res = await db.execute(stmt)
    count = res.rowcount

    if revoke_devices:
        dev_stmt = (
            update(Device)
            .where(Device.account_id == account_id, Device.status == "active")
            .values(status="revoked", revoked_at=now)
        )
        await db.execute(dev_stmt)

    await db.flush()
    return count


# ─── Scoped API Keys ──────────────────────────────────────────────────────────

def generate_api_key(account_id: uuid.UUID, name: str, scopes: list[str]) -> tuple[str, str, str]:
    """Generates a secure API key with prefix talos_sk_live_... Returns (raw_key, key_prefix, key_hash)."""
    raw_secret = secrets.token_urlsafe(32)
    key_prefix = f"talos_sk_{raw_secret[:6]}"
    raw_key = f"talos_sk_live_{raw_secret}"
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return raw_key, key_prefix, key_hash


async def create_api_key_record(
    db: AsyncSession,
    account_id: uuid.UUID,
    name: str,
    scopes: list[str] | None = None,
    expiry_days: int | None = None,
) -> tuple[str, ApiKey]:
    scope_str = ",".join(scopes) if scopes else "agent:run,workspace:read"
    raw_key, key_prefix, key_hash = generate_api_key(account_id, name, scopes or [])
    now = _utcnow()
    expires_at = now + timedelta(days=expiry_days) if expiry_days else None

    rec = ApiKey(
        key_id=uuid.uuid4(),
        account_id=account_id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=scope_str,
        created_at=now,
        expires_at=expires_at,
        revoked_at=None,
    )
    db.add(rec)
    await db.flush()
    return raw_key, rec


async def authenticate_api_key(db: AsyncSession, raw_key: str) -> tuple[Account, ApiKey] | None:
    if not raw_key.startswith("talos_sk_"):
        return None
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    stmt = select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
    res = await db.execute(stmt)
    key_rec = res.scalar_one_or_none()
    if not key_rec or key_rec.is_expired:
        return None

    account = await db.get(Account, key_rec.account_id)
    if not account or account.status != "active":
        return None

    key_rec.last_used_at = _utcnow()
    await db.flush()
    return account, key_rec


async def ensure_cloud_identity_schema(engine) -> None:
    """
    Idempotent schema bootstrapper ensuring all Cloud Identity V2 tables and columns
    exist across development, test, and production database environments.
    """
    from sqlalchemy import text

    async with engine.begin() as conn:
        dialect_name = conn.dialect.name

        # 1. Accounts columns
        if dialect_name == "postgresql":
            await conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS display_name VARCHAR(255)"))
            await conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500)"))
            await conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS publisher_slug VARCHAR(100)"))
            await conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS bio TEXT"))
            await conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS verified_publisher BOOLEAN NOT NULL DEFAULT FALSE"))
            await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_publisher_slug ON accounts(publisher_slug) WHERE publisher_slug IS NOT NULL"))

            # Marketplace listings economy columns
            await conn.execute(text("ALTER TABLE marketplace_listings ADD COLUMN IF NOT EXISTS pricing_type VARCHAR(20) NOT NULL DEFAULT 'free'"))
            await conn.execute(text("ALTER TABLE marketplace_listings ADD COLUMN IF NOT EXISTS price_credits INTEGER NOT NULL DEFAULT 0"))
            await conn.execute(text("ALTER TABLE marketplace_listings ADD COLUMN IF NOT EXISTS version_policy VARCHAR(50) NOT NULL DEFAULT 'all_minor_patch'"))
            await conn.execute(text("ALTER TABLE marketplace_listings ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT FALSE"))
            await conn.execute(text("ALTER TABLE marketplace_listings ADD COLUMN IF NOT EXISTS download_count INTEGER NOT NULL DEFAULT 0"))
            await conn.execute(text("ALTER TABLE marketplace_listings ADD COLUMN IF NOT EXISTS purchase_count INTEGER NOT NULL DEFAULT 0"))

            # Marketplace entitlements table
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS marketplace_entitlements (
                    entitlement_id UUID PRIMARY KEY,
                    account_id UUID NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    listing_id UUID NOT NULL REFERENCES marketplace_listings(listing_id) ON DELETE CASCADE,
                    purchase_id VARCHAR(128) UNIQUE NOT NULL,
                    price_paid_credits INTEGER NOT NULL DEFAULT 0,
                    version_policy VARCHAR(50) NOT NULL DEFAULT 'all_minor_patch',
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    acquired_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    CONSTRAINT uq_account_listing_entitlement UNIQUE (account_id, listing_id)
                )
            """))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_marketplace_entitlements_account ON marketplace_entitlements(account_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_marketplace_entitlements_listing ON marketplace_entitlements(listing_id)"))

            # Creator earnings ledger table
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS creator_earnings (
                    earning_id UUID PRIMARY KEY,
                    creator_id UUID NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    listing_id UUID NOT NULL REFERENCES marketplace_listings(listing_id) ON DELETE CASCADE,
                    version_id UUID REFERENCES marketplace_package_versions(version_id) ON DELETE SET NULL,
                    purchase_id VARCHAR(128) NOT NULL,
                    gross_credits INTEGER NOT NULL,
                    platform_share_credits INTEGER NOT NULL,
                    creator_share_credits INTEGER NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'available',
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
                )
            """))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_creator_earnings_creator ON creator_earnings(creator_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_creator_earnings_listing ON creator_earnings(listing_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_creator_earnings_purchase ON creator_earnings(purchase_id)"))

            # 2. identities table
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS identities (
                    identity_id UUID PRIMARY KEY,
                    account_id UUID NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    provider VARCHAR(50) NOT NULL,
                    provider_subject VARCHAR(255) NOT NULL,
                    email VARCHAR(255) NOT NULL,
                    email_verified BOOLEAN NOT NULL DEFAULT TRUE,
                    metadata_json TEXT,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    last_login_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    CONSTRAINT uq_provider_subject UNIQUE (provider, provider_subject)
                )
            """))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_identities_account_id ON identities(account_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_identities_email ON identities(email)"))

            # 3. devices table
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS devices (
                    device_id UUID PRIMARY KEY,
                    account_id UUID NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    device_name VARCHAR(255) NOT NULL,
                    platform VARCHAR(50) NOT NULL,
                    os_version VARCHAR(100),
                    app_version VARCHAR(50),
                    device_type VARCHAR(50) NOT NULL DEFAULT 'desktop',
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    revoked_at TIMESTAMP WITH TIME ZONE,
                    metadata_json TEXT
                )
            """))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_devices_account_id ON devices(account_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_devices_status ON devices(status)"))

            # 4. sessions table
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id UUID PRIMARY KEY,
                    account_id UUID NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    device_id UUID REFERENCES devices(device_id) ON DELETE SET NULL,
                    session_type VARCHAR(50) NOT NULL DEFAULT 'web',
                    refresh_token_hash VARCHAR(255) NOT NULL,
                    ip_address VARCHAR(100),
                    user_agent VARCHAR(500),
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    revoked_at TIMESTAMP WITH TIME ZONE,
                    metadata_json TEXT
                )
            """))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sessions_account_id ON sessions(account_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sessions_device_id ON sessions(device_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sessions_revoked_at ON sessions(revoked_at)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sessions_expires_at ON sessions(expires_at)"))

            # 5. api_keys table
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_id UUID PRIMARY KEY,
                    account_id UUID NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    name VARCHAR(255) NOT NULL,
                    key_prefix VARCHAR(16) NOT NULL,
                    key_hash VARCHAR(255) NOT NULL,
                    scopes VARCHAR(1000) NOT NULL DEFAULT 'agent:run,workspace:read',
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    expires_at TIMESTAMP WITH TIME ZONE,
                    last_used_at TIMESTAMP WITH TIME ZONE,
                    revoked_at TIMESTAMP WITH TIME ZONE
                )
            """))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_api_keys_account_id ON api_keys(account_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_api_keys_prefix ON api_keys(key_prefix)"))

            # 6. organizations table
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS organizations (
                    org_id UUID PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    slug VARCHAR(100) UNIQUE NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
                )
            """))

            # 7. organization_members table
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS organization_members (
                    id UUID PRIMARY KEY,
                    org_id UUID NOT NULL REFERENCES organizations(org_id) ON DELETE CASCADE,
                    account_id UUID NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    role VARCHAR(50) NOT NULL DEFAULT 'member',
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    CONSTRAINT uq_org_member UNIQUE (org_id, account_id)
                )
            """))

            # 8. projects table
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS projects (
                    project_id UUID PRIMARY KEY,
                    org_id UUID REFERENCES organizations(org_id) ON DELETE SET NULL,
                    name VARCHAR(255) NOT NULL,
                    slug VARCHAR(100) NOT NULL,
                    execution_mode VARCHAR(50) NOT NULL DEFAULT 'review',
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
                )
            """))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_projects_slug ON projects(slug)"))

            # 9. project_members table
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS project_members (
                    id UUID PRIMARY KEY,
                    project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    account_id UUID NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                    role VARCHAR(50) NOT NULL DEFAULT 'editor',
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    CONSTRAINT uq_project_member UNIQUE (project_id, account_id)
                )
            """))

