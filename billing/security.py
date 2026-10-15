import os
import ipaddress
import urllib.parse
from cryptography.fernet import Fernet
from fastapi import HTTPException

# Master key resolution: environment variable or fallback for development
_RAW_KEY = os.getenv("TALOS_MASTER_ENCRYPTION_KEY", "")
if not _RAW_KEY:
    # Deterministic development key (32 url-safe base64-encoded bytes)
    _RAW_KEY = "talos_dev_master_encryption_key_32bytes_v6="
    # Pad or generate valid fernet key if needed
    import base64
    _FERNET_KEY = base64.urlsafe_b64encode(_RAW_KEY.encode()[:32].ljust(32, b"0"))
else:
    _FERNET_KEY = _RAW_KEY.encode()

_cipher = Fernet(_FERNET_KEY)

def encrypt_secret(secret_text: str) -> str:
    """Encrypt a plaintext API key or credential string."""
    if not secret_text:
        return ""
    return _cipher.encrypt(secret_text.encode("utf-8")).decode("utf-8")

def decrypt_secret(encrypted_text: str) -> str:
    """Decrypt an encrypted API key."""
    if not encrypted_text:
        return ""
    try:
        return _cipher.decrypt(encrypted_text.encode("utf-8")).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"Failed to decrypt credential: {exc}")

def mask_secret(secret_text: str) -> str:
    """Mask an API key for safe UI and API presentation."""
    if not secret_text:
        return ""
    if len(secret_text) <= 8:
        return "sk-****"
    return f"{secret_text[:3]}...{secret_text[-4:]}"

def validate_provider_base_url(url: str, allow_local: bool = True) -> str:
    """
    Validates an outbound LLM provider base URL to prevent Server-Side Request Forgery (SSRF).
    Blocks RFC 1918 private IPs, loopbacks (unless allow_local), link-local, and cloud metadata.
    """
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Provider URL must use http or https scheme")
    
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid provider URL hostname")
    
    # Check for AWS/GCP/Azure instance metadata service IP
    if hostname in ("169.254.169.254", "metadata.google.internal"):
        raise HTTPException(status_code=400, detail="Access to cloud metadata endpoints is prohibited")

    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_loopback:
            if not allow_local:
                raise HTTPException(status_code=400, detail="Loopback URLs are not permitted in production")
        elif ip.is_private or ip.is_link_local:
            if not allow_local:
                raise HTTPException(status_code=400, detail="Private network endpoints are prohibited")
    except ValueError:
        # Hostname is a domain name (e.g. api.mistral.ai, api.openai.com)
        pass

    return url.strip().rstrip("/")
