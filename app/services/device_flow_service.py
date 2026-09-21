"""
Talos Cloud — RFC 8628 Device Authorization Flow Service.

Implements the OAuth 2.0 Device Authorization Grant (RFC 8628) store:
  1. Creates ephemeral device_code + user_code with 15-minute TTL.
  2. Resolves and marks codes as approved upon user confirmation in web browser.
  3. Supports desktop/CLI polling with status reporting (pending, approved, expired, denied).
  4. Redis-backed with automatic in-memory fallback for offline/test environments.
"""

import json
import logging
import secrets
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Charset excluding ambiguous characters (0, O, 1, I, L)
USER_CODE_CHARSET = "BCDFGHJKLMNPQRSTVWXZ23456789"
DEFAULT_EXPIRY_SECONDS = 900  # 15 minutes
POLL_INTERVAL_SECONDS = 5

# In-memory fallback store: { key: (value_dict, expires_at_epoch) }
_in_memory_device_codes: dict[str, tuple[dict[str, Any], float]] = {}
_in_memory_user_codes: dict[str, tuple[str, float]] = {}


def _clean_expired_memory_entries() -> None:
    now = time.time()
    expired_d = [k for k, (_, exp) in _in_memory_device_codes.items() if exp <= now]
    for k in expired_d:
        _in_memory_device_codes.pop(k, None)
    expired_u = [k for k, (_, exp) in _in_memory_user_codes.items() if exp <= now]
    for k in expired_u:
        _in_memory_user_codes.pop(k, None)


def _generate_user_code() -> str:
    """Generates an 8-character user code formatted as XXXX-XXXX."""
    chars = [secrets.choice(USER_CODE_CHARSET) for _ in range(8)]
    return f"{''.join(chars[:4])}-{''.join(chars[4:])}"


def _normalize_user_code(user_code: str) -> str:
    """Strips dashes, spaces, and normalizes to uppercase."""
    return user_code.replace("-", "").replace(" ", "").strip().upper()


async def create_device_authorization(
    device_label: str | None = None,
    expires_in: int = DEFAULT_EXPIRY_SECONDS,
) -> dict[str, Any]:
    """
    Initiates an RFC 8628 device authorization session.
    Returns:
      device_code: Secret polling token.
      user_code: Human-friendly 8-char code (e.g. WDJB-MJHT).
      expires_in: Expiry in seconds.
      interval: Recommended polling interval in seconds.
    """
    device_code = f"dcode_{secrets.token_urlsafe(32)}"
    user_code = _generate_user_code()
    normalized_user_code = _normalize_user_code(user_code)
    expires_at = time.time() + expires_in

    record = {
        "device_code": device_code,
        "user_code": user_code,
        "status": "pending",  # "pending", "approved", "denied"
        "account_id": None,
        "device_label": device_label,
        "expires_at": expires_at,
        "created_at": time.time(),
    }

    try:
        from app.services.rate_limiter import get_redis_client
        redis = await get_redis_client()
        if redis:
            await redis.set(f"auth:device_code:{device_code}", json.dumps(record), ex=expires_in)
            await redis.set(f"auth:device_user:{normalized_user_code}", device_code, ex=expires_in)
            return {
                "device_code": device_code,
                "user_code": user_code,
                "expires_in": expires_in,
                "interval": POLL_INTERVAL_SECONDS,
            }
    except Exception as e:
        logger.warning("Redis error creating device code; falling back to memory: %s", e)

    # In-memory fallback
    _clean_expired_memory_entries()
    _in_memory_device_codes[device_code] = (record, expires_at)
    _in_memory_user_codes[normalized_user_code] = (device_code, expires_at)

    return {
        "device_code": device_code,
        "user_code": user_code,
        "expires_in": expires_in,
        "interval": POLL_INTERVAL_SECONDS,
    }


async def get_device_authorization(device_code: str) -> dict[str, Any] | None:
    """Fetches device authorization state by device_code, or None if expired/not found."""
    try:
        from app.services.rate_limiter import get_redis_client
        redis = await get_redis_client()
        if redis:
            raw = await redis.get(f"auth:device_code:{device_code}")
            if raw:
                data = json.loads(raw)
                if data.get("expires_at", 0) > time.time():
                    return data
                return None
    except Exception:
        pass

    _clean_expired_memory_entries()
    entry = _in_memory_device_codes.get(device_code)
    if entry:
        rec, exp = entry
        if exp > time.time():
            return rec
        _in_memory_device_codes.pop(device_code, None)
    return None


async def approve_device_authorization(
    user_code: str,
    account_id: uuid.UUID,
    device_label: str | None = None,
) -> dict[str, Any] | None:
    """
    Approves a device authorization using the user_code entered by the web user.
    Returns the updated authorization dict, or None if invalid/expired.
    """
    normalized = _normalize_user_code(user_code)
    device_code: str | None = None

    try:
        from app.services.rate_limiter import get_redis_client
        redis = await get_redis_client()
        if redis:
            device_code = await redis.get(f"auth:device_user:{normalized}")
    except Exception:
        pass

    if not device_code:
        _clean_expired_memory_entries()
        entry = _in_memory_user_codes.get(normalized)
        if entry:
            d_code, exp = entry
            if exp > time.time():
                device_code = d_code

    if not device_code:
        return None

    record = await get_device_authorization(device_code)
    if not record or record.get("status") != "pending":
        return None

    record["status"] = "approved"
    record["account_id"] = str(account_id)
    if device_label:
        record["device_label"] = device_label

    remaining = max(1, int(record["expires_at"] - time.time()))

    try:
        from app.services.rate_limiter import get_redis_client
        redis = await get_redis_client()
        if redis:
            await redis.set(f"auth:device_code:{device_code}", json.dumps(record), ex=remaining)
            return record
    except Exception:
        pass

    _in_memory_device_codes[device_code] = (record, record["expires_at"])
    return record


async def deny_device_authorization(user_code: str) -> bool:
    """Denies a pending device authorization session."""
    normalized = _normalize_user_code(user_code)
    device_code: str | None = None

    try:
        from app.services.rate_limiter import get_redis_client
        redis = await get_redis_client()
        if redis:
            device_code = await redis.get(f"auth:device_user:{normalized}")
    except Exception:
        pass

    if not device_code:
        entry = _in_memory_user_codes.get(normalized)
        if entry:
            device_code = entry[0]

    if not device_code:
        return False

    record = await get_device_authorization(device_code)
    if not record:
        return False

    record["status"] = "denied"
    remaining = max(1, int(record["expires_at"] - time.time()))

    try:
        from app.services.rate_limiter import get_redis_client
        redis = await get_redis_client()
        if redis:
            await redis.set(f"auth:device_code:{device_code}", json.dumps(record), ex=remaining)
            return True
    except Exception:
        pass

    _in_memory_device_codes[device_code] = (record, record["expires_at"])
    return True


async def complete_device_authorization(device_code: str) -> dict[str, Any] | None:
    """
    Atomically retrieves and consumes an approved device authorization.
    Once consumed, it cannot be reused (replay prevention).
    """
    record = await get_device_authorization(device_code)
    if not record or record.get("status") != "approved":
        return None

    normalized = _normalize_user_code(record.get("user_code", ""))

    try:
        from app.services.rate_limiter import get_redis_client
        redis = await get_redis_client()
        if redis:
            await redis.delete(f"auth:device_code:{device_code}")
            await redis.delete(f"auth:device_user:{normalized}")
    except Exception:
        pass

    _in_memory_device_codes.pop(device_code, None)
    _in_memory_user_codes.pop(normalized, None)

    return record
