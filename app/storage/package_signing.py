"""
Talos Cloud — Package Signing & Integrity Verification (Task 14).

Uses Ed25519 asymmetric cryptography to guarantee publisher authenticity,
cryptographic non-repudiation, and tamper detection across all marketplace packages.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger("talos.storage.signing")


def generate_publisher_keypair() -> tuple[str, str]:
    """Generates a new Ed25519 keypair, returned as hex-encoded strings."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    priv_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv_raw.hex(), pub_raw.hex()


def sign_package_hash(sha256_hex: str, private_key_hex: str) -> str:
    """Signs the package SHA-256 hash using the publisher's Ed25519 private key."""
    priv_bytes = bytes.fromhex(private_key_hex)
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(priv_bytes)
    message = sha256_hex.strip().lower().encode("utf-8")
    signature = private_key.sign(message)
    return signature.hex()


def verify_package_signature(sha256_hex: str, signature_hex: str, public_key_hex: str) -> bool:
    """
    Verifies that the package SHA-256 hash was authentically signed by the
    publisher corresponding to the given Ed25519 public key.
    """
    try:
        pub_bytes = bytes.fromhex(public_key_hex)
        sig_bytes = bytes.fromhex(signature_hex)
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
        message = sha256_hex.strip().lower().encode("utf-8")
        public_key.verify(sig_bytes, message)
        return True
    except (InvalidSignature, ValueError, Exception) as e:
        logger.warning("Package signature verification failed: %s", e)
        return False


def verify_package_integrity(
    package_bytes: bytes,
    expected_sha256: str,
    signature_hex: Optional[str] = None,
    public_key_hex: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """
    Comprehensive tamper detection:
    1. Recomputes SHA-256 and compares with expected registry hash.
    2. If signature and public key are provided, validates Ed25519 cryptographic signature.
    Returns (is_valid, error_message).
    """
    actual_sha256 = hashlib.sha256(package_bytes).hexdigest()
    if actual_sha256.lower() != expected_sha256.strip().lower():
        return False, f"Tamper detected: calculated sha256 '{actual_sha256}' does not match expected '{expected_sha256}'"

    if signature_hex and public_key_hex:
        if not verify_package_signature(actual_sha256, signature_hex, public_key_hex):
            return False, "Cryptographic signature mismatch: package may have been forged or corrupted"

    return True, None


def sign_publisher_package(sha256_hex: str, publisher_private_key_hex: str) -> str:
    """Signs the package SHA-256 hash using the publisher's privately-held Ed25519 key."""
    return sign_package_hash(sha256_hex, publisher_private_key_hex)


def countersign_platform_package(sha256_hex: str, platform_private_key_hex: str) -> str:
    """Attaches a Talos Platform Ed25519 countersignature upon security approval."""
    return sign_package_hash(sha256_hex, platform_private_key_hex)


def verify_dual_signed_package(
    sha256_hex: str,
    publisher_signature_hex: str,
    publisher_public_key_hex: str,
    platform_signature_hex: Optional[str] = None,
    platform_public_key_hex: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """
    Verifies that the package was signed by the authentic publisher key,
    and optionally validated with the platform countersignature.
    """
    if not verify_package_signature(sha256_hex, publisher_signature_hex, publisher_public_key_hex):
        return False, "Publisher signature verification failed: invalid publisher key or forged package"

    if platform_signature_hex and platform_public_key_hex:
        if not verify_package_signature(sha256_hex, platform_signature_hex, platform_public_key_hex):
            return False, "Platform countersignature verification failed: package not approved by Talos Cloud"

    return True, None

