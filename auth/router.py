from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr
from typing import Optional

from auth.repository import auth_repo
from auth.service import (
    hash_password,
    verify_password,
    create_session_jwt,
    get_current_user,
    validate_csrf,
    COOKIE_NAME,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: Optional[str] = None

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

@router.post("/register")
async def register(req: RegisterRequest, response: Response, request: Request):
    validate_csrf(request)
    
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    existing = await auth_repo.get_user_by_email(req.email)
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    hashed_pw = hash_password(req.password)
    display_name = req.display_name or req.email.split("@")[0]
    user = await auth_repo.create_user(req.email, hashed_pw, display_name)

    # Create authoritative session and set HttpOnly cookie
    session = await auth_repo.create_session(user["id"])
    jwt_token = create_session_jwt(user["id"], session["id"])

    from config import config
    is_prod = config.TALOS_ENV == "production"

    response.set_cookie(
        key=COOKIE_NAME,
        value=jwt_token,
        httponly=True,
        samesite="lax",
        secure=is_prod,  # True in production HTTPS
        max_age=7 * 24 * 60 * 60,
        path="/"
    )

    is_admin = user.get("email", "").lower().strip() == "shikharjadav7@gmail.com"
    return {
        "status": "success",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "is_admin": is_admin
        }
    }

@router.post("/login")
async def login(req: LoginRequest, response: Response, request: Request):
    validate_csrf(request)

    user = await auth_repo.get_user_by_email(req.email)
    if not user or not verify_password(req.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Session rotation: create brand new session
    session = await auth_repo.create_session(user["id"])
    jwt_token = create_session_jwt(user["id"], session["id"])

    from config import config
    is_prod = config.TALOS_ENV == "production"

    response.set_cookie(
        key=COOKIE_NAME,
        value=jwt_token,
        httponly=True,
        samesite="lax",
        secure=is_prod,  # True in production HTTPS
        max_age=7 * 24 * 60 * 60,
        path="/"
    )

    is_admin = user.get("email", "").lower().strip() == "shikharjadav7@gmail.com"
    return {
        "status": "success",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "is_admin": is_admin
        }
    }

@router.post("/logout")
async def logout(response: Response, request: Request, current_user: dict = Depends(get_current_user)):
    validate_csrf(request)
    session_id = current_user.get("session_id")
    if session_id:
        await auth_repo.revoke_session(session_id)

    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"status": "success", "message": "Logged out successfully"}

@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return {
        "status": "success",
        "user": {
            "id": current_user["id"],
            "email": current_user["email"],
            "display_name": current_user.get("display_name"),
            "is_admin": current_user.get("is_admin", False)
        }
    }
