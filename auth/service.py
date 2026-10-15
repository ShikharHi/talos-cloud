import bcrypt
import jwt
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException, Security, status
from fastapi.security import APIKeyCookie
from auth.repository import auth_repo

JWT_SECRET = os.getenv("TALOS_JWT_SECRET", "talos_jwt_super_secure_secret_v6_2026")
JWT_ALGORITHM = "HS256"
COOKIE_NAME = "talos_session"

cookie_sec = APIKeyCookie(name=COOKIE_NAME, auto_error=False)

def hash_password(password: str) -> str:
    """Hash password using bcrypt."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password against bcrypt hash."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False

def create_session_jwt(user_id: str, session_id: str, duration_days: int = 7) -> str:
    """Create minimal JWT containing only sub and sid."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "sid": session_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=duration_days)).timestamp())
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_session_jwt(token: str) -> Optional[Dict[str, Any]]:
    """Decode and verify JWT signature."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception:
        return None

async def get_current_user(request: Request) -> Dict[str, Any]:
    """Dependency: Extract session from HttpOnly cookie and verify against database."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        # Also check Authorization header as fallback
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    if not token:
        # In local/development mode, auto-fallback to default user if unauthenticated
        from config import config
        if config.TALOS_ENV == "development":
            guest = await auth_repo.get_user_by_email("guest@talos.ai")
            if not guest:
                guest = await auth_repo.create_user(
                    email="guest@talos.ai",
                    hashed_pw=hash_password("GuestDev123!"),
                    display_name="Guest Developer"
                )
            return {**guest, "session_id": "ses_guest_dev"}
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )

    payload = decode_session_jwt(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token"
        )

    session_id = payload.get("sid")
    user_id = payload.get("sub")

    # Authoritative DB verification
    session = await auth_repo.get_valid_session(session_id)
    if not session or session.get("user_id") != user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been revoked or expired"
        )

    user = await auth_repo.get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found"
        )

    is_admin = user.get("email", "").lower().strip() == "shikharjadav7@gmail.com"
    return {**user, "session_id": session_id, "is_admin": is_admin}

def validate_csrf(request: Request):
    """CSRF protection for state-changing endpoints."""
    if request.method in ("POST", "DELETE", "PUT", "PATCH"):
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        allowed_origins = [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]
        # In development allow if origin or referer matches
        target = origin or referer
        if target:
            if not any(target.startswith(o) for o in allowed_origins):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF origin validation failed")
