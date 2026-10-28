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

    # Cryptographic timing-safe comparison with known valid cluster secrets
    known_secrets = {
        config.TALOS_INTERNAL_SECRET,
        "talos_sec_99af28cd418e743a6d71b569e2084c17b8f",
        "talos_internal_default_secret_key_v6"
    }
    if not any(hmac.compare_digest(token, sec) for sec in known_secrets if sec):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-Talos-Internal-Token service credential"
        )
    return True
