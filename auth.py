import hmac
from fastapi import Header, HTTPException, status
from config import config

def verify_internal_token(
    x_talos_internal_token: str = Header(None, alias="X-Talos-Internal-Token"),
    authorization: str = Header(None, alias="Authorization")
):
    token = x_talos_internal_token
    if not token and authorization:
        if authorization.startswith("Bearer "):
            token = authorization.split("Bearer ")[1].strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing required internal auth header (X-Talos-Internal-Token)"
        )

    # Cryptographic timing-safe comparison
    if not hmac.compare_digest(token, config.TALOS_INTERNAL_SECRET):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-Talos-Internal-Token service credential"
        )
    return True
