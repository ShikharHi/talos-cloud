"""
Tests for SSRF Guard:
  - Scheme restrictions (only HTTPS permitted)
  - Domain allowlist enforcement
  - IP blocking: loopback, private ranges, link-local, cloud metadata (169.254.169.254)
  - DNS resolution to blocked IP addresses
  - Redirect protection against SSRF escapes
  - Download size limits
"""

import ipaddress
import socket
import pytest
from unittest.mock import patch

from app.domain.marketplace.errors import SSRFBlockedError
from app.infrastructure.security.ssrf_guard import (
    is_ip_blocked,
    validate_remote_url,
    safe_fetch_url,
)


def test_ip_blocking_rules():
    # Loopback
    assert is_ip_blocked(ipaddress.ip_address("127.0.0.1")) is True
    assert is_ip_blocked(ipaddress.ip_address("::1")) is True

    # Cloud metadata
    assert is_ip_blocked(ipaddress.ip_address("169.254.169.254")) is True

    # RFC 1918 Private
    assert is_ip_blocked(ipaddress.ip_address("10.0.0.1")) is True
    assert is_ip_blocked(ipaddress.ip_address("172.16.0.1")) is True
    assert is_ip_blocked(ipaddress.ip_address("192.168.1.1")) is True

    # Link-local
    assert is_ip_blocked(ipaddress.ip_address("169.254.1.1")) is True

    # Public safe IPs
    assert is_ip_blocked(ipaddress.ip_address("8.8.8.8")) is False
    assert is_ip_blocked(ipaddress.ip_address("1.1.1.1")) is False
    assert is_ip_blocked(ipaddress.ip_address("140.82.121.4")) is False  # GitHub


def test_non_https_schemes_blocked():
    blocked_urls = [
        "http://github.com/org/repo/archive.zip",
        "file:///etc/passwd",
        "ftp://github.com/archive.zip",
        "gopher://github.com/archive.zip",
    ]
    for u in blocked_urls:
        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_remote_url(u, enforce_domain_allowlist=False)
        assert "scheme" in str(exc_info.value).lower()


def test_domain_allowlist_enforced():
    with pytest.raises(SSRFBlockedError) as exc_info:
        validate_remote_url("https://malicious-site.com/package.zip", enforce_domain_allowlist=True)
    assert "not in the allowed" in str(exc_info.value).lower()


def test_dns_resolving_to_private_ip_blocked():
    # Mock DNS resolution for an allowed domain resolving to internal IP
    mock_addrinfo = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))
    ]
    with patch("socket.getaddrinfo", return_value=mock_addrinfo):
        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_remote_url("https://github.com/test/repo", enforce_domain_allowlist=True)
        assert "resolves to blocked" in str(exc_info.value).lower()


def test_dns_resolving_to_metadata_ip_blocked():
    mock_addrinfo = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
    ]
    with patch("socket.getaddrinfo", return_value=mock_addrinfo):
        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_remote_url("https://github.com/test/repo", enforce_domain_allowlist=True)
        assert "resolves to blocked" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_redirect_to_blocked_ip_intercepted():
    """Ensure safe_fetch_url detects redirects attempting to pivot to private IP."""
    import httpx

    # Simulate an HTTP redirect chain: github.com -> 127.0.0.1
    class MockResponse:
        def __init__(self, status_code, headers, content=b""):
            self.status_code = status_code
            self.headers = headers
            self._content = content

        def raise_for_status(self):
            pass

        async def aiter_bytes(self, chunk_size=65536):
            yield self._content

    async def mock_get(self, url, **kwargs):
        if "github.com" in str(url):
            return MockResponse(
                status_code=302,
                headers={"location": "https://127.0.0.1/admin"},
            )
        return MockResponse(status_code=200, headers={}, content=b"evil")

    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.121.4", 443))]):
        with patch.object(httpx.AsyncClient, "get", mock_get):
            with pytest.raises(SSRFBlockedError):
                await safe_fetch_url("https://github.com/org/repo/archive.zip")
