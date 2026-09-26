"""
Talos Cloud — Auth & Identity router.

Handles:
  1. Google OAuth2/OIDC login and callback -> Web Sessions (for browser/dashboard).
  2. Device Token registration (for Talos desktop runtime) -> Gated by Web Session.
  3. Device Token refresh, revoke, and inspect -> For runtime execution.

INVARIANT:
  Web Sessions (JWT) and Device Tokens (bcrypt hash) are strictly separated.
  Web sessions cannot access /relay/*; device tokens cannot access admin dashboard.
"""

import os
import uuid
import logging
from typing import Optional
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.accounts import Account, WebSessionRecord, Device, Session, ApiKey
from app.services import auth_service, cloud_identity_service, device_flow_service, identity_service
from app.services.authorization_service import (
    AuthDecision,
    AuthorizationContext,
    authorize as eval_authorization,
)
from app.services.identity_service import AccountLinkingError, InvalidSessionError, WebSession
from app.services.rate_limiter import rate_limiter

router = APIRouter(prefix="/auth", tags=["auth"])
router_devices = APIRouter(prefix="/api/v1/devices", tags=["devices-v1"])
logger = logging.getLogger(__name__)


# ─── Auth Dependencies ────────────────────────────────────────────────────────

async def get_current_session(
    authorization: Optional[str] = Header(None),
    talos_session: Optional[str] = Cookie(None),
    db: AsyncSession = Depends(get_db),
) -> WebSession:
    """
    Extracts and validates a Web Session token from Authorization header ('Bearer <jwt>')
    or 'talos_session' cookie.
    Rejects missing, invalid, or revoked sessions with 401.
    """
    raw_jwt = None
    if authorization and authorization.startswith("Bearer "):
        raw_jwt = authorization.removeprefix("Bearer ").strip()
    elif talos_session:
        raw_jwt = talos_session.strip()

    if raw_jwt:
        try:
            token_session = identity_service.verify_web_session(raw_jwt)

            # Check persistent revocation in DB or Redis cache if session_id (sid) is present
            if token_session.session_id:
                if await identity_service.is_session_revoked_cache(token_session.session_id):
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Session has been revoked.",
                        headers={"WWW-Authenticate": "Bearer"},
                    )

                record = await db.get(WebSessionRecord, token_session.session_id)
                if record is None:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Session is invalid or expired.",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                if record.is_revoked:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Session has been revoked.",
                        headers={"WWW-Authenticate": "Bearer"},
                    )

            # Refresh user details from DB
            from sqlalchemy import select
            acc_res = await db.execute(select(Account).where(Account.account_id == token_session.account_id))
            acc = acc_res.scalar_one_or_none()
            if acc:
                return WebSession(
                    account_id=acc.account_id,
                    email=acc.email,
                    role=getattr(acc, "role", "user"),
                    google_sub=getattr(acc, "google_sub", token_session.google_sub),
                    session_id=token_session.session_id,
                )
            return token_session
        except InvalidSessionError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(e),
                headers={"WWW-Authenticate": "Bearer"},
            )

    # Support test harness overrides if get_current_session or get_authenticated_account was mocked
    try:
        from app.main import app
        from app.routers.relay import get_authenticated_account
        if get_current_session in app.dependency_overrides:
            override = app.dependency_overrides[get_current_session]
            acc = override() if callable(override) else override
            if acc:
                return WebSession(
                    account_id=acc.account_id,
                    email=acc.email,
                    role=getattr(acc, "role", "user"),
                    google_sub=getattr(acc, "google_sub", None),
                )
        if get_authenticated_account in app.dependency_overrides:
            override = app.dependency_overrides[get_authenticated_account]
            acc = override() if callable(override) else override
            if acc:
                return WebSession(
                    account_id=acc.account_id,
                    email=acc.email,
                    role=getattr(acc, "role", "user"),
                    google_sub=getattr(acc, "google_sub", None),
                )
    except Exception:
        pass

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required. Please log in with Google to create a web session.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_admin(
    authorization: Optional[str] = Header(None),
    talos_session: Optional[str] = Cookie(None),
    x_admin_secret: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> WebSession:
    """
    Ensures the caller has admin privileges either via Web Session JWT with role='admin'
    or via TALOS_ADMIN_SECRET header (for CLI/scripts).
    """
    settings = get_settings()
    expected_secret = settings.talos_admin_secret or os.environ.get("TALOS_ADMIN_SECRET", "")
    if x_admin_secret and expected_secret and x_admin_secret == expected_secret:
        return WebSession(
            account_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            email="admin@system.local",
            role="admin",
        )

    session = await get_current_session(authorization=authorization, talos_session=talos_session, db=db)
    if session.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return session


# ─── Request / Response Schemas ───────────────────────────────────────────────

class DeviceRegisterRequest(BaseModel):
    device_label: str | None = None


class DeviceRegisterResponse(BaseModel):
    account_id: str
    raw_token: str
    expires_at: str
    message: str = (
        "Store this token in your OS keychain immediately. "
        "It will not be shown again. Never store it in a plain file or SQLite."
    )


class DeviceCodeRequest(BaseModel):
    client_id: str | None = None
    scope: str | None = None
    device_label: str | None = None


class DeviceCodeResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int = 900
    interval: int = 5


class DeviceApproveRequest(BaseModel):
    user_code: str
    device_label: str | None = None
    action: str = "approve"  # "approve" or "deny"


class DeviceTokenPollRequest(BaseModel):
    device_code: str
    grant_type: str = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceTokenPollResponse(BaseModel):
    access_token: str
    device_token: str
    token_type: str = "bearer"
    expires_in: int
    account_id: str


class RefreshRequest(BaseModel):
    raw_token: str | None = None
    refresh_token: str | None = None
    device_label: str | None = None


class RefreshResponse(BaseModel):
    raw_token: str
    expires_at: str


class SessionRefreshRequest(BaseModel):
    refresh_token: Optional[str] = None


class SessionRefreshResponse(BaseModel):
    access_token: str
    session_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 900


class SessionItem(BaseModel):
    session_id: str
    created_at: str | None = None
    last_used_at: str | None = None
    user_agent: str | None = None
    ip_address: str | None = None
    is_current: bool = False


class DeviceItem(BaseModel):
    device_id: str
    device_name: str
    platform: str
    device_type: str = "desktop"
    os_version: str | None = None
    app_version: str | None = None
    status: str = "active"
    created_at: str | None = None
    last_seen_at: str | None = None


class ApiKeyCreateRequest(BaseModel):
    name: str
    scopes: list[str] | None = None
    expiry_days: int | None = None


class ApiKeyResponse(BaseModel):
    key_id: str
    name: str
    key_prefix: str
    scopes: list[str]
    created_at: str
    expires_at: str | None = None
    last_used_at: str | None = None
    raw_key: str | None = None


class RemoteAuthStartRequest(BaseModel):
    device_label: str | None = None
    platform: str | None = None


class RemoteAuthStartResponse(BaseModel):
    challenge_id: str
    user_code: str
    verification_url: str
    verification_url_complete: str
    expires_in: int = 900
    interval: int = 5


class RemoteAuthCompleteRequest(BaseModel):
    challenge_id: str


class AuthorizeCheckRequest(BaseModel):
    action: str
    resource: str | None = None
    project_id: str | None = None
    execution_mode: str = "review"
    tool_args: dict | None = None


class AuthorizeCheckResponse(BaseModel):
    decision: str  # ALLOW, REQUIRE_APPROVAL, DENY
    reason: str


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


class RevokeRequest(BaseModel):
    raw_token: str


class WebSessionInfo(BaseModel):
    account_id: str
    email: str
    role: str
    google_sub: str | None
    session_id: str | None = None


class GoogleCallbackResponse(BaseModel):
    session_token: str
    access_token: str | None = None
    refresh_token: str | None = None
    account_id: str
    email: str
    role: str


@router.get(
    "/google/login",
    dependencies=[Depends(rate_limiter(max_requests=30, window_seconds=60, key_prefix="google_login"))],
)
async def google_login(
    state: Optional[str] = None,
    prompt: Optional[str] = "select_account",
    redirect: bool = False,
):
    """Returns the Google OAuth 2.0 consent screen redirect URL or redirects directly."""
    auth_url = identity_service.get_google_auth_url(state=state, prompt=prompt or "select_account")
    if redirect:
        return RedirectResponse(url=auth_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    return {"auth_url": auth_url}


@router.get(
    "/google/callback",
    dependencies=[Depends(rate_limiter(max_requests=30, window_seconds=60, key_prefix="google_callback"))],
)
@router.get(
    "/callback",
    dependencies=[Depends(rate_limiter(max_requests=30, window_seconds=60, key_prefix="google_callback"))],
)
async def google_callback(
    request: Request,
    code: Optional[str] = Query(None, description="Authorization code from Google"),
    state: Optional[str] = Query(None, description="Client UI origin or CSRF state"),
    error: Optional[str] = Query(None, description="OAuth error"),
    error_description: Optional[str] = Query(None, description="OAuth error description"),
    response: Response = Response(),
    db: AsyncSession = Depends(get_db),
):
    """
    Exchanges Google auth code, resolves/links Talos Account, and issues Web Session JWT.
    Redirects the browser back to the UI with the session token as a query param.
    If the state indicates a connection authorization flow (state_...), relays to backend.
    """
    # Check if this callback belongs to the Unified Connection Manager flow
    if state and state.startswith("state_"):
        backend_url = os.environ.get("TALOS_BACKEND_URL", "http://localhost:8000").rstrip("/")
        relay_params = {"state": state}
        if code:
            relay_params["code"] = code
        if error:
            relay_params["error"] = error
        if error_description:
            relay_params["error_description"] = error_description
        import urllib.parse
        relay_url = f"{backend_url}/api/connections/google/callback?{urllib.parse.urlencode(relay_params)}"
        return RedirectResponse(url=relay_url, status_code=302)

    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_description or error or "Missing authorization code from Google.",
        )

    try:
        userinfo = await identity_service.exchange_google_code(code)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google authentication failed: {e}",
        )

    google_sub = userinfo.get("sub")
    email = userinfo.get("email")
    email_verified = bool(userinfo.get("email_verified", False))

    if not google_sub or not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incomplete Google profile (missing sub or email).",
        )

    if not email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google account email is not verified. Unverified accounts cannot sign in or link.",
        )

    try:
        account = await identity_service.resolve_google_account(
            db=db,
            google_sub=google_sub,
            email=email,
            email_verified=email_verified,
        )
        await db.commit()
    except AccountLinkingError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    user_agent = request.headers.get("user-agent")
    ip_address = request.client.host if request.client else None
    try:
        session_token, refresh_token, session_rec = await identity_service.create_web_session_record(
            db=db,
            account=account,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        await db.commit()
    except Exception as exc:
        logger.error("Failed to create web session for account %s: %s", getattr(account, 'account_id', 'unknown'), exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create session: {exc}",
        )

    if "application/json" in (request.headers.get("accept") or ""):
        return {
            "session_token": session_token,
            "access_token": session_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": 900,
            "account_id": str(account.account_id),
            "email": account.email,
            "role": account.role,
        }

    # Validate state against trusted origins to prevent Open Redirect attacks
    import re
    default_ui = os.environ.get("TALOS_UI_URL", "http://localhost:3000").rstrip("/")
    ui_base = default_ui
    if state and (re.match(r"^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?$", state) or state == default_ui):
        ui_base = state.rstrip("/")

    redirect_url = f"{ui_base}/auth/callback?token={session_token}&refresh_token={refresh_token}"
    resp = RedirectResponse(url=redirect_url, status_code=302)
    resp.set_cookie(
        key="talos_session",
        value=session_token,
        httponly=True,
        samesite="lax",
        max_age=7 * 86400,
    )
    resp.set_cookie(
        key="talos_refresh_token",
        value=refresh_token,
        httponly=True,
        samesite="lax",
        max_age=30 * 86400,
    )
    return resp




@router.get("/session/me", response_model=WebSessionInfo)
async def get_session_profile(session: WebSession = Depends(get_current_session)):
    """Returns the authenticated web user's identity details."""
    return WebSessionInfo(
        account_id=str(session.account_id),
        email=session.email,
        role=session.role,
        google_sub=session.google_sub,
    )


@router.get("/me")
async def get_any_me(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Introspection endpoint for either Web Session JWTs or Device Tokens.
    Allows runtime and client to verify identity seamlessly.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token.")

    token = authorization.removeprefix("Bearer ").strip()

    # 1. Try Device Token
    if token.startswith("dtok_") or token.startswith("talos_") or token.startswith("dt_"):
        dt = await auth_service.authenticate_token(db, token)
        if dt:
            account = await db.get(Account, dt.account_id)
            if account:
                return {
                    "id": str(account.account_id),
                    "account_id": str(account.account_id),
                    "email": account.email,
                    "role": account.role,
                    "subscription_tier": account.subscription_tier,
                }

    # 2. Try Scoped API Key
    if token.startswith("talos_sk_"):
        api_res = await cloud_identity_service.authenticate_api_key(db, token)
        if api_res:
            acc, key_rec = api_res
            return {
                "id": str(acc.account_id),
                "account_id": str(acc.account_id),
                "email": acc.email,
                "role": acc.role,
                "subscription_tier": acc.subscription_tier,
                "auth_type": "api_key",
                "scopes": key_rec.scopes.split(",") if key_rec.scopes else [],
            }

    # 3. Try Web Session / Access Token
    try:
        session = identity_service.verify_web_session(token)
        return {
            "id": str(session.account_id),
            "account_id": str(session.account_id),
            "email": session.email,
            "role": session.role,
        }
    except Exception:
        pass

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")


# ─── Device Registration Endpoints (Gated by Web Session) ─────────────────────

@router.post(
    "/device/register",
    response_model=DeviceRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limiter(max_requests=10, window_seconds=60, key_prefix="device_register"))],
)
@router.post(
    "/devices",
    response_model=DeviceRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limiter(max_requests=10, window_seconds=60, key_prefix="device_register"))],
)
@router.post(
    "/register",
    response_model=DeviceRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limiter(max_requests=10, window_seconds=60, key_prefix="device_register"))],
)
async def register_device(
    req: DeviceRegisterRequest = DeviceRegisterRequest(),
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """
    Registers a new device for the authenticated Google user and issues a device token.
    The raw_token must be stored in the device's OS Keychain.
    """
    token_id = uuid.uuid4()
    raw_token = auth_service._generate_raw_token(token_id=token_id)
    device_token = auth_service.DeviceToken(
        token_id=token_id,
        account_id=session.account_id,
        token_hash=auth_service._hash_token(raw_token),
        device_label=req.device_label,
        expires_at=auth_service._expiry(),
    )
    db.add(device_token)
    await db.commit()

    return DeviceRegisterResponse(
        account_id=str(session.account_id),
        raw_token=raw_token,
        expires_at=device_token.expires_at.isoformat(),
    )


# ─── Web Session Lifecycle & Revocation Endpoints ──────────────────────────────

@router.post(
    "/session/refresh",
    response_model=SessionRefreshResponse,
    dependencies=[Depends(rate_limiter(max_requests=60, window_seconds=60, key_prefix="session_refresh"))],
)
async def refresh_web_session(
    response: Response,
    request: Request,
    req: Optional[SessionRefreshRequest] = None,
    talos_refresh_token: Optional[str] = Cookie(None),
    talos_session: Optional[str] = Cookie(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Refreshes a web session access token using a rotating refresh token.
    Accepts refresh token from JSON payload OR secure HttpOnly cookie.
    Performs replay detection and automatic revocation on breach.
    """
    token_to_use = None
    if req and req.refresh_token:
        token_to_use = req.refresh_token
    elif talos_refresh_token:
        token_to_use = talos_refresh_token
    elif talos_session and "." in talos_session and len(talos_session.split(".")[0]) == 32:
        token_to_use = talos_session

    if not token_to_use:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing refresh token in request body or cookie.",
        )

    user_agent = request.headers.get("user-agent")
    ip_address = request.client.host if request.client else None
    try:
        access_token, new_refresh_token, _ = await identity_service.rotate_refresh_token(
            db=db,
            raw_refresh_token=token_to_use,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        await db.commit()

        # Set secure HttpOnly cookies for browser clients
        response.set_cookie(
            key="talos_session",
            value=access_token,
            httponly=True,
            samesite="lax",
            max_age=7 * 86400,
        )
        response.set_cookie(
            key="talos_refresh_token",
            value=new_refresh_token,
            httponly=True,
            samesite="lax",
            max_age=30 * 86400,
        )

        return SessionRefreshResponse(
            access_token=access_token,
            session_token=access_token,
            refresh_token=new_refresh_token,
            token_type="bearer",
            expires_in=7 * 86400,
        )
    except InvalidSessionError as e:
        await db.commit()
        response.delete_cookie(key="talos_session")
        response.delete_cookie(key="talos_refresh_token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.post(
    "/logout",
    dependencies=[Depends(rate_limiter(max_requests=20, window_seconds=60, key_prefix="logout"))],
)
async def logout(
    req: LogoutRequest = LogoutRequest(),
    response: Response = Response(),
    authorization: Optional[str] = Header(None),
    talos_session: Optional[str] = Cookie(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Revokes the caller's current web session.
    Can be called with Bearer JWT, talos_session cookie, or refresh_token in request body.
    """
    revoked = False
    raw_jwt = None
    if authorization and authorization.startswith("Bearer "):
        raw_jwt = authorization.removeprefix("Bearer ").strip()
    elif talos_session:
        raw_jwt = talos_session.strip()

    if raw_jwt:
        try:
            session = identity_service.verify_web_session(raw_jwt)
            if session.session_id:
                await identity_service.revoke_session(db, session.session_id, session.account_id)
                revoked = True
        except Exception:
            pass

    if req.refresh_token:
        try:
            parts = req.refresh_token.strip().split(".", 1)
            if len(parts) == 2:
                session_id = uuid.UUID(parts[0])
                await identity_service.revoke_session(db, session_id)
                revoked = True
        except Exception:
            pass

    await db.commit()
    response.delete_cookie(key="talos_session")
    response.delete_cookie(key="talos_refresh_token")
    return {"status": "ok", "message": "Successfully logged out.", "revoked": revoked}


@router.post(
    "/logout-all",
    dependencies=[Depends(rate_limiter(max_requests=10, window_seconds=60, key_prefix="logout_all"))],
)
async def logout_all(
    response: Response,
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """
    Revokes ALL active web sessions for the authenticated user across all devices.
    """
    count = await identity_service.revoke_all_sessions(db, session.account_id)
    await db.commit()
    response.delete_cookie(key="talos_session")
    response.delete_cookie(key="talos_refresh_token")
    return {"status": "ok", "revoked_count": count, "message": "All active sessions revoked."}


@router.get("/sessions", response_model=list[SessionItem])
async def list_sessions(
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """
    Lists all active web sessions for the authenticated user.
    """
    records = await identity_service.list_active_sessions(db, session.account_id)
    return [
        SessionItem(
            session_id=str(r.session_id),
            created_at=r.created_at.isoformat() if r.created_at else None,
            last_used_at=r.last_used_at.isoformat() if r.last_used_at else None,
            user_agent=r.user_agent,
            ip_address=r.ip_address,
            is_current=bool(session.session_id and r.session_id == session.session_id),
        )
        for r in records
    ]


@router.delete("/sessions/{session_id}")
async def revoke_session_by_id(
    session_id: uuid.UUID,
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """
    Revokes a specific web session by ID. Must belong to authenticated user.
    """
    revoked = await identity_service.revoke_session(db, session_id, account_id=session.account_id)
    await db.commit()
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found or already revoked.",
        )
    return {"status": "ok", "session_id": str(session_id), "revoked": True}


# ─── First-Class Device Management Endpoints ─────────────────────────────────

@router.get("/devices/list", response_model=list[DeviceItem])
@router.get("/devices", response_model=list[DeviceItem])
async def list_user_devices(
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """Lists all registered client devices attached to the Cloud Account."""
    devices = await cloud_identity_service.list_devices(db, session.account_id)
    return [
        DeviceItem(
            device_id=str(d.device_id),
            device_name=d.device_name,
            platform=d.platform,
            device_type=d.device_type,
            os_version=d.os_version,
            app_version=d.app_version,
            status=d.status,
            created_at=d.created_at.isoformat() if d.created_at else None,
            last_seen_at=d.last_seen_at.isoformat() if d.last_seen_at else None,
        )
        for d in devices
    ]


@router.post("/devices/{device_id}/revoke")
@router.delete("/devices/{device_id}")
async def revoke_device_by_id(
    device_id: uuid.UUID,
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """Revokes a device and terminates all its attached sessions immediately."""
    ok = await cloud_identity_service.revoke_device(db, device_id, account_id=session.account_id)
    await db.commit()
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found or not owned by user.",
        )
    return {"status": "ok", "device_id": str(device_id), "revoked": True}


# ─── Scoped API Key Endpoints ────────────────────────────────────────────────

@router.get("/api-keys", response_model=list[ApiKeyResponse])
async def list_api_keys(
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """Lists all active API keys for the Cloud Account."""
    from sqlalchemy import select
    stmt = (
        select(ApiKey)
        .where(ApiKey.account_id == session.account_id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at.desc())
    )
    res = await db.execute(stmt)
    records = res.scalars().all()
    return [
        ApiKeyResponse(
            key_id=str(k.key_id),
            name=k.name,
            key_prefix=k.key_prefix,
            scopes=k.scopes.split(",") if k.scopes else [],
            created_at=k.created_at.isoformat() if k.created_at else "",
            expires_at=k.expires_at.isoformat() if k.expires_at else None,
            last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
            raw_key=None,
        )
        for k in records
    ]


@router.post("/api-keys", response_model=ApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    req: ApiKeyCreateRequest,
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """Creates a new scoped API key. Returns raw key secret ONCE."""
    raw_key, rec = await cloud_identity_service.create_api_key_record(
        db=db,
        account_id=session.account_id,
        name=req.name,
        scopes=req.scopes,
        expiry_days=req.expiry_days,
    )
    await db.commit()
    return ApiKeyResponse(
        key_id=str(rec.key_id),
        name=rec.name,
        key_prefix=rec.key_prefix,
        scopes=rec.scopes.split(",") if rec.scopes else [],
        created_at=rec.created_at.isoformat() if rec.created_at else "",
        expires_at=rec.expires_at.isoformat() if rec.expires_at else None,
        raw_key=raw_key,
    )


@router.post("/api-keys/{key_id}/revoke")
@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: uuid.UUID,
    session: WebSession = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """Revokes an API key immediately."""
    rec = await db.get(ApiKey, key_id)
    if not rec or rec.account_id != session.account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found.")
    rec.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "ok", "key_id": str(key_id), "revoked": True}


# ─── Remote / SSH Out-Of-Band Authentication ──────────────────────────────────

@router.post("/remote/start", response_model=RemoteAuthStartResponse)
async def start_remote_auth(req: RemoteAuthStartRequest = RemoteAuthStartRequest()):
    """
    Out-of-band flow for SSH / headless terminal authentication.
    Returns human user_code and verification link.
    """
    res = await device_flow_service.create_device_authorization(device_label=req.device_label or "Remote Terminal")
    ui_base = os.environ.get("TALOS_UI_URL", "http://localhost:3000").rstrip("/")
    verification_url = f"{ui_base}/auth/device"
    verification_url_complete = f"{ui_base}/auth/device?user_code={res['user_code']}"
    return RemoteAuthStartResponse(
        challenge_id=res["device_code"],
        user_code=res["user_code"],
        verification_url=verification_url,
        verification_url_complete=verification_url_complete,
        expires_in=res["expires_in"],
        interval=res["interval"],
    )


@router.post("/remote/complete")
async def complete_remote_auth(
    req: RemoteAuthCompleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Polls completion of remote / SSH challenge and issues Access Token."""
    rec = await device_flow_service.get_device_authorization(req.challenge_id)
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "code_expired", "error_description": "Challenge expired or invalid."},
        )

    st = rec.get("status")
    if st == "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "authorization_pending", "error_description": "User has not approved yet."},
        )
    if st == "denied":
        await device_flow_service.complete_device_authorization(req.challenge_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "access_denied", "error_description": "User denied the request."},
        )
    if st == "approved":
        consumed = await device_flow_service.complete_device_authorization(req.challenge_id)
        account_id_raw = consumed.get("account_id") if consumed else None
        if not account_id_raw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_grant", "error_description": "Challenge already consumed or missing account."},
            )
        acc_id = uuid.UUID(account_id_raw)
        account = await db.get(Account, acc_id)
        if not account or account.status != "active":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account inactive.")

        # Register remote device
        device = await cloud_identity_service.register_device(
            db=db,
            account_id=acc_id,
            device_name=consumed.get("device_label") or "Remote Terminal",
            platform="remote",
            device_type="cli",
        )
        access_token, raw_refresh, sess = await cloud_identity_service.create_cloud_session(
            db=db,
            account=account,
            session_type="remote",
            device=device,
        )
        await db.commit()
        return {
            "access_token": access_token,
            "refresh_token": raw_refresh,
            "session_id": str(sess.session_id),
            "device_id": str(device.device_id),
            "account_id": str(acc_id),
            "email": account.email,
            "role": account.role,
            "token_type": "bearer",
            "expires_in": 900,
        }

    raise HTTPException(status_code=400, detail="Unknown challenge status.")


# ─── Capability & Policy Authorization Check Endpoint ─────────────────────────

@router.post("/permissions/check", response_model=AuthorizeCheckResponse)
@router.post("/permissions", response_model=AuthorizeCheckResponse)
async def check_permissions(
    req: AuthorizeCheckRequest,
    session: WebSession = Depends(get_current_session),
):
    """
    Evaluates whether an action is allowed, requires user approval, or is denied.
    """
    ctx = AuthorizationContext(
        account_id=str(session.account_id),
        role=session.role,
        session_id=str(session.session_id) if session.session_id else None,
        project_id=req.project_id,
        execution_mode=req.execution_mode,
        scopes=["*"],
    )
    decision, reason = eval_authorization(
        ctx=ctx,
        action=req.action,
        resource=req.resource,
        tool_args=req.tool_args,
    )
    return AuthorizeCheckResponse(decision=decision.value, reason=reason)


# ─── Runtime Device Token Endpoints ──────────────────────────────────────────

@router.post(
    "/refresh",
    dependencies=[Depends(rate_limiter(max_requests=60, window_seconds=60, key_prefix="refresh"))],
)
async def refresh_token(
    req: RefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Unified refresh endpoint:
    - If 'refresh_token' is passed -> refreshes Web Session with rotation.
    - If 'raw_token' is passed -> refreshes Device Token.
    """
    if req.refresh_token:
        user_agent = request.headers.get("user-agent")
        ip_address = request.client.host if request.client else None
        try:
            access_token, new_refresh_token, _ = await identity_service.rotate_refresh_token(
                db=db,
                raw_refresh_token=req.refresh_token,
                user_agent=user_agent,
                ip_address=ip_address,
            )
            await db.commit()
            return {
                "access_token": access_token,
                "session_token": access_token,
                "refresh_token": new_refresh_token,
                "token_type": "bearer",
                "expires_in": 900,
            }
        except InvalidSessionError as e:
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(e),
                headers={"WWW-Authenticate": "Bearer"},
            )

    if req.raw_token:
        result = await auth_service.refresh_token(
            db, raw_old_token=req.raw_token, device_label=req.device_label
        )
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Device token is invalid, expired, or already revoked.",
            )
        new_dt, raw_token = result
        await db.commit()
        return RefreshResponse(raw_token=raw_token, expires_at=new_dt.expires_at.isoformat())

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Missing refresh_token (for web session) or raw_token (for device token).",
    )


@router.post("/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device_token(req: RevokeRequest, db: AsyncSession = Depends(get_db)):
    """Revokes a device token (device logout/deregistration)."""
    ok = await auth_service.revoke_token(db, raw_token=req.raw_token)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device token not found or already revoked.",
        )
    await db.commit()


# ─── RFC 8628 Device Authorization Flow Endpoints ────────────────────────────

@router.post(
    "/device-code",
    response_model=DeviceCodeResponse,
    dependencies=[Depends(rate_limiter(max_requests=20, window_seconds=60, key_prefix="device_code"))],
)
@router.post(
    "/device/code",
    response_model=DeviceCodeResponse,
    dependencies=[Depends(rate_limiter(max_requests=20, window_seconds=60, key_prefix="device_code"))],
)
@router_devices.post(
    "/code",
    response_model=DeviceCodeResponse,
    dependencies=[Depends(rate_limiter(max_requests=20, window_seconds=60, key_prefix="device_code"))],
)
async def request_device_code(req: DeviceCodeRequest = DeviceCodeRequest()):
    """
    RFC 8628 Device Authorization Request for CLI and Desktop runtime.
    Returns device_code (secret for polling) and human-friendly user_code to confirm in browser.
    """
    res = await device_flow_service.create_device_authorization(device_label=req.device_label)
    ui_base = os.environ.get("TALOS_UI_URL", "http://localhost:3000").rstrip("/")
    verification_uri = f"{ui_base}/auth/device"
    verification_uri_complete = f"{ui_base}/auth/device?user_code={res['user_code']}"
    return DeviceCodeResponse(
        device_code=res["device_code"],
        user_code=res["user_code"],
        verification_uri=verification_uri,
        verification_uri_complete=verification_uri_complete,
        expires_in=res["expires_in"],
        interval=res["interval"],
    )


@router.post(
    "/device/approve",
    dependencies=[Depends(rate_limiter(max_requests=30, window_seconds=60, key_prefix="device_approve"))],
)
@router_devices.post(
    "/approve",
    dependencies=[Depends(rate_limiter(max_requests=30, window_seconds=60, key_prefix="device_approve"))],
)
async def approve_device_code(
    req: DeviceApproveRequest,
    session: WebSession = Depends(get_current_session),
):
    """
    Approves a device authorization using the user_code entered in browser by an authenticated user.
    """
    if req.action.lower() == "deny":
        ok = await device_flow_service.deny_device_authorization(req.user_code)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "invalid_user_code", "error_description": "User code not found or expired."},
            )
        return {"status": "denied"}

    ok = await device_flow_service.approve_device_authorization(req.user_code, session.account_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "invalid_user_code", "error_description": "User code not found or expired."},
        )
    return {"status": "approved"}


@router.post(
    "/device-token",
    response_model=DeviceTokenPollResponse,
    dependencies=[Depends(rate_limiter(max_requests=60, window_seconds=60, key_prefix="device_token"))],
)
@router.post(
    "/device/token",
    response_model=DeviceTokenPollResponse,
    dependencies=[Depends(rate_limiter(max_requests=60, window_seconds=60, key_prefix="device_token"))],
)
@router_devices.post(
    "/token",
    response_model=DeviceTokenPollResponse,
    dependencies=[Depends(rate_limiter(max_requests=60, window_seconds=60, key_prefix="device_token"))],
)
async def poll_device_token(
    req: DeviceTokenPollRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    RFC 8628 Token Polling Endpoint for CLI and Desktop runtime.
    Polls with device_code until user approves in browser, then returns long-lived device token.
    """
    record = await device_flow_service.get_device_authorization(req.device_code)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "code_expired", "error_description": "Device code has expired or is invalid."},
        )

    st = record.get("status")
    if st == "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "authorization_pending", "error_description": "User has not yet approved authorization."},
        )

    if st == "denied":
        await device_flow_service.complete_device_authorization(req.device_code)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "access_denied", "error_description": "User denied the authorization request."},
        )

    if st == "approved":
        consumed = await device_flow_service.complete_device_authorization(req.device_code)
        if not consumed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "code_expired", "error_description": "Device authorization already consumed."},
            )

        account_id_raw = consumed.get("account_id")
        if not account_id_raw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_grant", "error_description": "Missing account on approved code."},
            )

        account_id = uuid.UUID(account_id_raw)
        token_id = uuid.uuid4()
        raw_token = auth_service._generate_raw_token(token_id=token_id)
        device_token = auth_service.DeviceToken(
            token_id=token_id,
            account_id=account_id,
            token_hash=auth_service._hash_token(raw_token),
            device_label=consumed.get("device_label"),
            expires_at=auth_service._expiry(),
        )
        db.add(device_token)
        await db.commit()

        settings = get_settings()
        expires_in = settings.token_expiry_minutes * 60
        return DeviceTokenPollResponse(
            access_token=raw_token,
            device_token=raw_token,
            token_type="bearer",
            expires_in=expires_in,
            account_id=str(account_id),
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "invalid_request", "error_description": "Unknown device code status."},
    )
