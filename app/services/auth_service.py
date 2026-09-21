"""
Talos Cloud — Auth Service.

Token contract:
  - Raw token is issued exactly ONCE per registration or refresh.
    It is returned to the client in the response body and NEVER re-sent.
  - The server stores ONLY a bcrypt hash of the raw token.
  - The raw token cannot be re-derived from the stored hash.
  - Client must store the raw token in OS keychain (not plain file or SQLite).
  - Tokens are short-lived (default 60 minutes); clients refresh via heartbeat.
  - On refresh, the old token is invalidated atomically with the new token insert.
"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.accounts import Account, DeviceToken


def _hash_token(raw: str) -> str:
    """Bcrypt-hash the raw token for server-side storage."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(raw.encode("utf-8")[:72], salt).decode("utf-8")


def _verify_token(raw: str, hashed: str) -> bool:
    """Verify raw token against stored bcrypt hash."""
    try:
        return bcrypt.checkpw(raw.encode("utf-8")[:72], hashed.encode("utf-8"))
    except Exception:
        return False


def _generate_raw_token(token_id: uuid.UUID | None = None) -> str:
    """Generates a cryptographically secure URL-safe token.
    If token_id is provided, prefixes it as dtok_{token_id.hex}_{secret}
    to allow instant O(1) indexed primary key lookup on the server.
    """
    secret = secrets.token_urlsafe(32)
    if token_id is not None:
        return f"dtok_{token_id.hex}_{secret}"
    return f"dtok_{secret}"


def _expiry() -> datetime:
    settings = get_settings()
    return datetime.now(timezone.utc) + timedelta(minutes=settings.token_expiry_minutes)


# ─── Redis Caching Helpers for Device Tokens ───────────────────────────────────

async def _cache_token(token_id: uuid.UUID, account_id: uuid.UUID, expires_at: datetime) -> None:
    """Caches authenticated token state in Redis to avoid subsequent DB queries."""
    try:
        from app.services.rate_limiter import get_redis_client
        redis = await get_redis_client()
        if redis:
            now = datetime.now(timezone.utc)
            remaining = int((expires_at - now).total_seconds())
            ttl = max(1, min(remaining, 300))
            await redis.set(f"auth:token:{token_id.hex}", str(account_id), ex=ttl)
    except Exception:
        pass


async def _get_cached_account_id(token_id: uuid.UUID) -> uuid.UUID | None:
    """Returns cached account_id UUID from Redis if present."""
    try:
        from app.services.rate_limiter import get_redis_client
        redis = await get_redis_client()
        if redis:
            cached = await redis.get(f"auth:token:{token_id.hex}")
            if cached:
                return uuid.UUID(cached)
    except Exception:
        pass
    return None


async def _invalidate_token_cache(token_id: uuid.UUID) -> None:
    """Evicts token state from Redis upon revocation or refresh."""
    try:
        from app.services.rate_limiter import get_redis_client
        redis = await get_redis_client()
        if redis:
            await redis.delete(f"auth:token:{token_id.hex}")
    except Exception:
        pass


async def register_account(
    db: AsyncSession,
    email: str,
    device_label: str | None = None,
    default_free_credits: int = 10,
) -> tuple[Account, str]:
    """
    Creates a new account and issues an initial device token.
    Returns (account, raw_token).
    raw_token is returned ONCE here; the server never re-derives it.
    The caller must return it to the client exactly once.
    """
    account = Account(email=email)
    db.add(account)
    await db.flush()  # get account_id without committing

    if default_free_credits > 0:
        from app.services import ledger_service
        await ledger_service.grant_subscription_credits(
            db=db,
            account_id=account.account_id,
            credits=default_free_credits,
            cycle_ref=f"welcome_signup_{account.account_id.hex[:6]}",
        )
        await db.refresh(account)

    token_id = uuid.uuid4()
    raw_token = _generate_raw_token(token_id=token_id)
    device_token = DeviceToken(
        token_id=token_id,
        account_id=account.account_id,
        token_hash=_hash_token(raw_token),
        device_label=device_label,
        expires_at=_expiry(),
    )
    db.add(device_token)
    await db.flush()
    return account, raw_token


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def authenticate_token(
    db: AsyncSession,
    raw_token: str,
) -> DeviceToken | None:
    """
    Validates a raw device token. Returns the DeviceToken row if valid,
    None if expired, revoked, or not found.

    Fast Path (O(1)):
      When token has format dtok_{token_id_hex}_{secret}, we query DeviceToken
      directly by primary key (token_id). If found, we verify the bcrypt hash.
      This replaces the O(N) sequential table scan with a single O(1) indexed lookup.

    Fallback Path (O(N)):
      For legacy / un-prefixed tokens (e.g. from older clients or unit tests),
      we scan recent non-revoked tokens for backward compatibility.
    """
    if not raw_token or not isinstance(raw_token, str) or not raw_token.startswith("dtok_"):
        return None

    now = datetime.now(timezone.utc)

    # Check for embedded token_id in format dtok_{token_id.hex}_{secret}
    parts = raw_token.split("_")
    if len(parts) >= 3 and len(parts[1]) == 32:
        try:
            token_id = uuid.UUID(parts[1])
            # Direct Primary Key lookup - O(1)
            row = await db.get(DeviceToken, token_id)
            if row is not None and not row.revoked and _ensure_utc(row.expires_at) > now:
                if _verify_token(raw_token, row.token_hash):
                    await _cache_token(row.token_id, row.account_id, _ensure_utc(row.expires_at))
                    return row
            return None
        except ValueError:
            pass

    # Fallback: scan recent non-revoked tokens (Phase 1 legacy format compatibility)
    result = await db.execute(
        select(DeviceToken).where(
            DeviceToken.revoked.is_(False),
            DeviceToken.expires_at > now,
        )
    )
    for row in result.scalars():
        if _verify_token(raw_token, row.token_hash):
            await _cache_token(row.token_id, row.account_id, row.expires_at)
            return row
    return None


async def refresh_token(
    db: AsyncSession,
    raw_old_token: str,
    device_label: str | None = None,
) -> tuple[DeviceToken, str] | None:
    """
    Invalidates the old token and issues a new one.
    Returns (new_device_token, raw_new_token) or None if old token is invalid.
    The raw new token is returned ONCE; the server never re-derives it.
    """
    old_dt = await authenticate_token(db, raw_old_token)
    if old_dt is None:
        return None

    # Revoke old token and invalidate cache
    old_dt.revoked = True
    await _invalidate_token_cache(old_dt.token_id)

    token_id = uuid.uuid4()
    raw_token = _generate_raw_token(token_id=token_id)
    new_dt = DeviceToken(
        token_id=token_id,
        account_id=old_dt.account_id,
        token_hash=_hash_token(raw_token),
        device_label=device_label or old_dt.device_label,
        expires_at=_expiry(),
    )
    db.add(new_dt)
    await db.flush()
    return new_dt, raw_token


async def revoke_token(
    db: AsyncSession,
    raw_token: str,
) -> bool:
    """Revokes a device token. Returns True if the token was found and revoked."""
    dt = await authenticate_token(db, raw_token)
    if dt is None:
        return False
    dt.revoked = True
    await _invalidate_token_cache(dt.token_id)
    return True


async def get_account_for_token(
    db: AsyncSession,
    raw_token: str,
) -> Account | None:
    """Returns the Account associated with a valid raw token, or None."""
    # Check cache for O(1) account lookup
    if raw_token and isinstance(raw_token, str) and raw_token.startswith("dtok_"):
        parts = raw_token.split("_")
        if len(parts) >= 3 and len(parts[1]) == 32:
            try:
                tid = uuid.UUID(parts[1])
                cached_acc_id = await _get_cached_account_id(tid)
                if cached_acc_id:
                    acc = await db.get(Account, cached_acc_id)
                    if acc and acc.status == "active":
                        return acc
                    else:
                        await _invalidate_token_cache(tid)
            except Exception:
                pass

    dt = await authenticate_token(db, raw_token)
    if dt is None:
        return None
    result = await db.execute(
        select(Account).where(Account.account_id == dt.account_id)
    )
    return result.scalar_one_or_none()
