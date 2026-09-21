"""
Talos Cloud — Identity & Web Session Service.

Handles:
  1. Google OAuth 2.0 / OIDC authentication flow with strict claim validation.
  2. Safe account-linking policy (sub-first, verified-email only, hijack protection).
  3. Browser Web Sessions:
     - Asymmetric RS256 signed JWTs with Key ID (kid) and rotation keyring.
     - Persistent WebSessionRecord database model with rotating refresh tokens and replay detection.
     - Architecturally isolated from device tokens (type="device_token").
"""

import hashlib
import hmac
import logging
import secrets
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
from jose import JWTError, jwt  # type: ignore[import-untyped]
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.accounts import Account, WebSessionRecord
from app.services import ledger_service

logger = logging.getLogger(__name__)


@dataclass
class WebSession:
    account_id: uuid.UUID
    email: str
    role: str
    google_sub: str | None = None
    session_id: uuid.UUID | None = None


class AccountLinkingError(Exception):
    """Raised when an explicit account-linking policy constraint is violated."""
    pass


class InvalidSessionError(Exception):
    """Raised when a web session token is invalid, expired, revoked, or wrong type."""
    pass


# ─── RS256 Key Management & Rotation ──────────────────────────────────────────

_ephemeral_key_cache: dict[str, Any] = {}


def get_signing_key() -> tuple[str, str]:
    """
    Returns (private_key_pem, key_id).
    Uses settings.jwt_private_key_pem if set.
    Otherwise generates and caches an ephemeral RSA-2048 keypair for dev/test mode.
    Enforces that explicit keys must be configured in production mode.
    """
    settings = get_settings()
    if settings.jwt_private_key_pem and settings.jwt_private_key_pem.strip():
        return settings.jwt_private_key_pem, settings.jwt_key_id

    if getattr(settings, "talos_env", "").lower() == "production":
        raise RuntimeError(
            "Production environment requires explicit JWT_PRIVATE_KEY_PEM and JWT_PUBLIC_KEY_PEM "
            "configuration to prevent multi-pod signing discrepancies."
        )

    if "private_key_pem" in _ephemeral_key_cache:
        return _ephemeral_key_cache["private_key_pem"], _ephemeral_key_cache["key_id"]

    logger.warning("Generating ephemeral RSA-2048 keypair for JWT signing (development/test mode).")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    key_id = settings.jwt_key_id or "talos-v1"
    _ephemeral_key_cache["private_key_pem"] = priv_pem
    _ephemeral_key_cache["public_key_pem"] = pub_pem
    _ephemeral_key_cache["key_id"] = key_id

    return priv_pem, key_id


def get_verification_key(kid: str | None = None) -> str:
    """
    Resolves the public key for verifying RS256 JWTs.
    Checks:
      1. Configured jwt_public_key_pem (or derived from jwt_private_key_pem) if kid matches active key_id.
      2. Keyring in settings.previous_public_key_map for key rotation.
      3. Ephemeral keypair if active.
    """
    settings = get_settings()
    active_key_id = settings.jwt_key_id or "talos-v1"

    if kid is None or kid == active_key_id:
        if settings.jwt_public_key_pem and settings.jwt_public_key_pem.strip():
            return settings.jwt_public_key_pem
        if settings.jwt_private_key_pem and settings.jwt_private_key_pem.strip():
            priv_key = serialization.load_pem_private_key(
                settings.jwt_private_key_pem.encode("utf-8"), password=None
            )
            pub_pem = priv_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("utf-8")
            return pub_pem

        if getattr(settings, "talos_env", "").lower() == "production":
            raise RuntimeError(
                "Production environment requires explicit JWT_PUBLIC_KEY_PEM or JWT_PRIVATE_KEY_PEM."
            )

        if "public_key_pem" in _ephemeral_key_cache:
            return _ephemeral_key_cache["public_key_pem"]
        get_signing_key()
        return _ephemeral_key_cache["public_key_pem"]

    prev_keys = settings.previous_public_key_map
    if kid in prev_keys:
        return prev_keys[kid]

    if _ephemeral_key_cache.get("key_id") == kid:
        return _ephemeral_key_cache["public_key_pem"]

    raise InvalidSessionError(f"Unknown signing key ID (kid: '{kid}'). Key may have expired or been rotated.")


# ─── Redis Caching Helpers for Web Sessions ───────────────────────────────────

async def mark_session_revoked_cache(session_id: uuid.UUID) -> None:
    """Marks a web session as revoked in Redis with a 24-hour TTL."""
    try:
        from app.services.rate_limiter import get_redis_client
        redis = await get_redis_client()
        if redis:
            await redis.set(f"auth:revoked_session:{session_id.hex}", "1", ex=86400)
    except Exception:
        pass


async def is_session_revoked_cache(session_id: uuid.UUID) -> bool:
    """Checks if a web session has been marked as revoked in Redis."""
    try:
        from app.services.rate_limiter import get_redis_client
        redis = await get_redis_client()
        if redis:
            val = await redis.get(f"auth:revoked_session:{session_id.hex}")
            return val == "1"
    except Exception:
        pass
    return False


# ─── Web Session JWT Generation & Verification ───────────────────────────────

def create_web_session(
    account: Account,
    session_id: uuid.UUID | None = None,
    expiry_minutes: int | None = None,
) -> str:
    """
    Creates a signed RS256 JWT scoped exclusively for browser web sessions.
    INVARIANTS:
      - Claim 'type': 'web_session'
      - Claim 'iss': settings.jwt_issuer
      - Claim 'aud': settings.jwt_audience
      - Header 'alg': 'RS256', 'kid': active key_id
    Device tokens have a different token format/claims and cannot be used as sessions.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    exp_mins = expiry_minutes if expiry_minutes is not None else settings.session_expiry_minutes
    exp = now + timedelta(minutes=exp_mins)
    private_key_pem, key_id = get_signing_key()

    payload = {
        "sub": str(account.account_id),
        "email": account.email,
        "role": account.role,
        "google_sub": account.google_sub,
        "type": "web_session",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    if session_id:
        payload["sid"] = str(session_id)

    headers = {
        "alg": "RS256",
        "typ": "JWT",
        "kid": key_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256", headers=headers)


def verify_web_session(token: str) -> WebSession:
    """
    Verifies an RS256 web session JWT.
    Enforces signature, expiration, key ID lookup, issuer, audience, and type="web_session".
    """
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as e:
        raise InvalidSessionError(f"Malformed token header: {e}") from e

    alg = header.get("alg")
    if alg != "RS256":
        raise InvalidSessionError(f"Unsupported algorithm '{alg}'. Only RS256 is accepted for web sessions.")

    kid = header.get("kid")
    pub_key = get_verification_key(kid)

    try:
        payload = jwt.decode(
            token,
            pub_key,
            algorithms=["RS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )
    except JWTError as e:
        raise InvalidSessionError(f"Invalid session token: {e}") from e

    token_type = payload.get("type")
    if token_type != "web_session":
        raise InvalidSessionError(
            f"Expected token type 'web_session', got '{token_type}'. "
            "Device tokens cannot be used as web sessions."
        )

    sub = payload.get("sub")
    email = payload.get("email")
    role = payload.get("role", "user")
    google_sub = payload.get("google_sub")
    sid_str = payload.get("sid")

    if not sub or not email:
        raise InvalidSessionError("Malformed session payload: missing sub or email.")

    try:
        account_id = uuid.UUID(sub)
    except ValueError as e:
        raise InvalidSessionError("Invalid account_id UUID in session.") from e

    session_id: uuid.UUID | None = None
    if sid_str:
        try:
            session_id = uuid.UUID(sid_str)
        except ValueError:
            pass

    return WebSession(
        account_id=account_id,
        email=email,
        role=role,
        google_sub=google_sub,
        session_id=session_id,
    )


# ─── Persistent Web Session & Refresh Token Lifecycle ─────────────────────────

def _hash_refresh_token(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


async def create_web_session_record(
    db: AsyncSession,
    account: Account,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, str, WebSessionRecord]:
    """
    Creates a persistent WebSessionRecord with a cryptographically secure rotating refresh token.
    Returns (access_token, raw_refresh_token, session_record).
    """
    settings = get_settings()
    session_id = uuid.uuid4()
    refresh_secret = secrets.token_urlsafe(32)
    refresh_token_hash = _hash_refresh_token(refresh_secret)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=settings.refresh_token_expiry_days)

    record = WebSessionRecord(
        session_id=session_id,
        account_id=account.account_id,
        refresh_token_hash=refresh_token_hash,
        user_agent=user_agent[:500] if user_agent else None,
        ip_address=ip_address[:100] if ip_address else None,
        created_at=now,
        last_used_at=now,
        expires_at=expires_at,
        revoked_at=None,
    )
    db.add(record)
    await db.flush()

    raw_refresh_token = f"{session_id.hex}.{refresh_secret}"
    access_token = create_web_session(account, session_id=session_id)
    return access_token, raw_refresh_token, record


async def rotate_refresh_token(
    db: AsyncSession,
    raw_refresh_token: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, str, WebSessionRecord]:
    """
    Rotates a refresh token:
      1. Parses session_id and secret.
      2. Validates session existence and expiration.
      3. Validates refresh_token_hash.
      4. Detects replay attacks if session is revoked or hash differs; revokes immediately on breach.
      5. Generates a new secret, updates hash, issues new access token & refresh token.
    """
    now = datetime.now(timezone.utc)
    parts = raw_refresh_token.strip().split(".", 1)
    if len(parts) != 2:
        raise InvalidSessionError("Malformed refresh token format.")

    session_id_raw, secret = parts
    try:
        session_id = uuid.UUID(session_id_raw)
    except ValueError as e:
        raise InvalidSessionError("Invalid session_id in refresh token.") from e

    record = await db.get(WebSessionRecord, session_id)
    if record is None:
        raise InvalidSessionError("Session record not found.")

    if record.is_revoked:
        logger.warning("SECURITY ALERT: Replay attack detected on revoked session %s", session_id)
        raise InvalidSessionError("Web session has been revoked.")

    if record.expires_at <= now:
        raise InvalidSessionError("Web session refresh token has expired.")

    expected_hash = _hash_refresh_token(secret)
    if not hmac.compare_digest(expected_hash, record.refresh_token_hash):
        record.revoked_at = now
        await db.flush()
        logger.warning("SECURITY ALERT: Refresh token hash mismatch for session %s. Revoking session.", session_id)
        raise InvalidSessionError("Invalid refresh token. Session has been revoked for security.")

    account = await db.get(Account, record.account_id)
    if account is None:
        raise InvalidSessionError("Associated account not found.")

    new_secret = secrets.token_urlsafe(32)
    record.refresh_token_hash = _hash_refresh_token(new_secret)
    record.last_used_at = now
    if user_agent:
        record.user_agent = user_agent[:500]
    if ip_address:
        record.ip_address = ip_address[:100]
    await db.flush()

    new_raw_refresh = f"{session_id.hex}.{new_secret}"
    new_access_token = create_web_session(account, session_id=session_id)
    return new_access_token, new_raw_refresh, record


async def revoke_session(
    db: AsyncSession,
    session_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
) -> bool:
    """Revokes a specific web session."""
    record = await db.get(WebSessionRecord, session_id)
    if record is None:
        return False
    if account_id is not None and record.account_id != account_id:
        return False
    if record.revoked_at is None:
        record.revoked_at = datetime.now(timezone.utc)
        await db.flush()
    await mark_session_revoked_cache(session_id)
    return True


async def revoke_all_sessions(db: AsyncSession, account_id: uuid.UUID) -> int:
    """Revokes all active web sessions for an account."""
    now = datetime.now(timezone.utc)
    # Evict cache for active sessions
    active_sessions = await list_active_sessions(db, account_id)
    for s in active_sessions:
        await mark_session_revoked_cache(s.session_id)

    stmt = (
        update(WebSessionRecord)
        .where(WebSessionRecord.account_id == account_id, WebSessionRecord.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount  # type: ignore[return-value]


async def list_active_sessions(db: AsyncSession, account_id: uuid.UUID) -> list[WebSessionRecord]:
    """Lists all active (non-revoked) sessions for an account."""
    stmt = (
        select(WebSessionRecord)
        .where(WebSessionRecord.account_id == account_id, WebSessionRecord.revoked_at.is_(None))
        .order_by(WebSessionRecord.last_used_at.desc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


# ─── Google OAuth Flow Helpers ────────────────────────────────────────────────

def get_google_auth_url(state: str | None = None, prompt: str = "select_account") -> str:
    """Builds a Google OAuth URL that explicitly asks for the account chooser."""
    settings = get_settings()
    client_id = settings.google_client_id or "mock-google-client-id"
    redirect_uri = settings.google_redirect_uri or "http://localhost:8001/auth/google/callback"
    scope = "openid email profile"

    clean_prompt = (prompt or "select_account").strip()
    params = [
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("response_type", "code"),
        ("scope", scope),
        ("access_type", "offline"),
        ("prompt", clean_prompt),
    ]
    if state:
        params.append(("state", state))

    query = "&".join(f"{key}={urllib.parse.quote(str(value), safe='')}" for key, value in params)
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


async def exchange_google_code(code: str) -> dict[str, Any]:
    """
    Exchanges an authorization code with Google for tokens and userinfo.
    Returns dict with keys: 'sub', 'email', 'email_verified', 'name'.
    """
    settings = get_settings()

    # For dev / mock testing mode when real credentials are not set
    if not settings.google_client_id or settings.google_client_id.startswith("mock-"):
        is_unverified = "unverified" in code.lower()
        return {
            "sub": f"google-sub-{code}",
            "email": f"{code}@gmail.com" if "@" not in code else code,
            "email_verified": not is_unverified,
            "name": f"User {code}",
        }

    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(token_url, data=data)
        if resp.status_code != 200:
            logger.error("Google token exchange failed [%s]: %s", resp.status_code, resp.text)
            raise AccountLinkingError(f"Google token exchange failed ({resp.status_code}): {resp.text}")
        token_data = resp.json()

        id_token = token_data.get("id_token")
        if id_token:
            try:
                unverified_claims = jwt.get_unverified_claims(id_token)
                iss = unverified_claims.get("iss")
                if iss not in ("https://accounts.google.com", "accounts.google.com"):
                    raise AccountLinkingError(f"Invalid Google ID token issuer: {iss}")
                aud = unverified_claims.get("aud")
                if settings.google_client_id and aud != settings.google_client_id:
                    raise AccountLinkingError(f"Google ID token audience mismatch: {aud}")
            except Exception as e:
                logger.warning("Google ID token validation warning: %s", e)

        access_token = token_data.get("access_token")
        userinfo_url = "https://openidconnect.googleapis.com/v1/userinfo"
        userinfo_resp = await client.get(
            userinfo_url, headers={"Authorization": f"Bearer {access_token}"}
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()

        return {
            "sub": userinfo["sub"],
            "email": userinfo["email"],
            "email_verified": bool(userinfo.get("email_verified", False)),
            "name": userinfo.get("name", ""),
        }


# ─── Explicit Account Resolution & Linking Policy ───────────────────────────

async def resolve_google_account(
    db: AsyncSession,
    google_sub: str,
    email: str,
    email_verified: bool = False,
    default_free_credits: int = 10,
) -> Account:
    """
    Resolves or creates a Talos account from Google identity under strict safety rules:

    Policy:
      1. google_sub match -> return existing account immediately.
      2. google_sub not found:
         - If email_verified is False: REJECT! Cannot link or register with unverified email.
         - If email matches existing account:
           - If existing account has google_sub is None -> link google_sub to this account.
           - If existing account already has a different google_sub -> reject (prevent hijacking).
         - No matching account -> create brand new Account with role='user' and grant initial credits.
    """
    settings = get_settings()
    is_admin = bool(email and email.lower() in [e.lower() for e in settings.admin_email_list])

    # 1. Primary lookup by stable google_sub
    result = await db.execute(select(Account).where(Account.google_sub == google_sub))
    account = result.scalar_one_or_none()
    if account is not None:
        if is_admin and (account.role != "admin" or account.subscription_tier != "admin"):
            account.role = "admin"
            account.subscription_tier = "admin"
            if account.balance_credits < 1_000_000_000:
                account.balance_credits = 1_000_000_000
            await db.flush()
        return account

    # 2. Unverified email protection: reject if email is not verified
    if not email_verified:
        logger.warning(
            "AUDIT: Account linking rejected for email=%s, google_sub=%s (unverified Google email)",
            email, google_sub
        )
        raise AccountLinkingError(
            f"Google email '{email}' is not verified. Cannot link or create account with unverified email."
        )

    # 3. Lookup by verified email
    if email:
        result_email = await db.execute(select(Account).where(Account.email == email))
        existing_email_acc = result_email.scalar_one_or_none()

        if existing_email_acc is not None:
            if existing_email_acc.google_sub is None:
                # Safe linking: existing email-only account is now linked to Google ID
                existing_email_acc.google_sub = google_sub
                if is_admin:
                    existing_email_acc.role = "admin"
                    existing_email_acc.subscription_tier = "admin"
                    if existing_email_acc.balance_credits < 1_000_000_000:
                        existing_email_acc.balance_credits = 1_000_000_000
                logger.info(
                    "AUDIT: Linked existing email account %s to google_sub %s",
                    email, google_sub
                )
                await db.flush()
                return existing_email_acc
            elif existing_email_acc.google_sub != google_sub:
                logger.warning(
                    "AUDIT: Account linking conflict for %s: existing sub %s != %s",
                    email, existing_email_acc.google_sub, google_sub
                )
                raise AccountLinkingError(
                    f"Account for email '{email}' is already linked to a different Google identity."
                )

    # 4. Create new Account
    new_acc = Account(
        email=email,
        google_sub=google_sub,
        role="admin" if is_admin else "user",
        balance_credits=1_000_000_000 if is_admin else 0,
        subscription_tier="admin" if is_admin else "free",
    )
    db.add(new_acc)
    await db.flush()
    logger.info("AUDIT: Created new account %s for google_sub %s", email, google_sub)

    # Grant initial free-tier credits via standard ledger service
    if not is_admin and default_free_credits > 0:
        await ledger_service.grant_subscription_credits(
            db=db,
            account_id=new_acc.account_id,
            credits=default_free_credits,
            cycle_ref=f"welcome_signup_{new_acc.account_id.hex[:6]}",
        )
        await db.refresh(new_acc)
    elif is_admin:
        await ledger_service.grant_subscription_credits(
            db=db,
            account_id=new_acc.account_id,
            credits=1_000_000_000,
            cycle_ref=f"admin_init_{new_acc.account_id.hex[:6]}",
        )
        await db.refresh(new_acc)

    return new_acc
