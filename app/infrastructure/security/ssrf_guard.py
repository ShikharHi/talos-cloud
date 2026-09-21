"""
Talos Cloud — SSRF Protection & Safe Remote Fetcher.

Defends against SSRF, metadata service access (169.254.169.254),
private IP probing, and DNS rebinding during remote package/repo imports.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse
import httpx

from app.domain.marketplace.errors import SSRFBlockedError

ALLOWED_IMPORT_DOMAINS = {
    "github.com",
    "raw.githubusercontent.com",
    "api.github.com",
    "gitlab.com",
}


def is_ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """
    Returns True if the IP address belongs to loopback, private, link-local,
    reserved, or cloud metadata ranges.
    """
    if ip.is_loopback:
        return True
    if ip.is_private:
        return True
    if ip.is_link_local:
        return True
    if ip.is_multicast:
        return True
    if ip.is_reserved:
        return True
    if ip.is_unspecified:
        return True

    # Cloud metadata check (169.254.169.254)
    if isinstance(ip, ipaddress.IPv4Address):
        if str(ip) == "169.254.169.254":
            return True

    return False


def validate_remote_url(url: str, enforce_domain_allowlist: bool = True) -> str:
    """
    Validates that a URL is safe to fetch from the server:
    1. Scheme must be HTTPS
    2. Hostname must resolve to public IP addresses only
    3. Rejects loopback, link-local, RFC 1918, and metadata IPs
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise SSRFBlockedError(f"Forbidden URL scheme '{parsed.scheme}'. Only HTTPS is permitted.")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFBlockedError("URL missing hostname.")

    if enforce_domain_allowlist and hostname.lower() not in ALLOWED_IMPORT_DOMAINS:
        raise SSRFBlockedError(
            f"Domain '{hostname}' is not in the allowed marketplace import list: {sorted(list(ALLOWED_IMPORT_DOMAINS))}."
        )

    # Resolve hostname to all IP addresses
    try:
        addr_info = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SSRFBlockedError(f"Could not resolve hostname '{hostname}': {e}")

    for entry in addr_info:
        ip_str = entry[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
            if is_ip_blocked(ip):
                raise SSRFBlockedError(f"Hostname '{hostname}' resolves to blocked private/local IP: '{ip_str}'.")
        except ValueError:
            raise SSRFBlockedError(f"Invalid IP address resolved for '{hostname}': '{ip_str}'.")

    return url


async def safe_fetch_url(
    url: str,
    max_size: int = 52_428_800,
    timeout_s: float = 30.0,
    enforce_domain_allowlist: bool = True,
) -> bytes:
    """
    Safely downloads remote data enforcing SSRF checks on initial URL and on any redirects.
    """
    current_url = validate_remote_url(url, enforce_domain_allowlist=enforce_domain_allowlist)
    total_downloaded = 0
    buffer = bytearray()

    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
        redirects = 0
        while redirects < 5:
            resp = await client.get(current_url, headers={"User-Agent": "Talos-Marketplace-Importer/1.0"})

            # Handle redirects safely
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise SSRFBlockedError("Redirect response missing Location header.")
                redirects += 1
                # Validate redirect destination
                current_url = validate_remote_url(location, enforce_domain_allowlist=enforce_domain_allowlist)
                continue

            resp.raise_for_status()

            # Stream response body with size limit
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                total_downloaded += len(chunk)
                if total_downloaded > max_size:
                    raise SSRFBlockedError(f"Download size exceeded maximum limit of {max_size} bytes.")
                buffer.extend(chunk)

            return bytes(buffer)

        raise SSRFBlockedError("Too many redirects during remote import.")
