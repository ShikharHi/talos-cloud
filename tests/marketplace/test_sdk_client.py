"""
End-to-end tests for talos_agent_sdk MarketplaceClient with talos-cloud:
  - MarketplaceClient.search()
  - MarketplaceClient.get()
  - MarketplaceClient.install() with 2-phase protocol, SHA-256 validation, and staging
  - MarketplaceClient.uninstall()
"""

import uuid
import pytest
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.models.accounts import Account
from app.models.marketplace import MarketplaceListing, MarketplacePackageVersion
from app.storage.service import StorageService, reset_storage_service
from tests.test_storage_provider import FakeStorageProvider
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from talos_agent_sdk.marketplace import MarketplaceClient


@pytest.fixture
def fake_storage(monkeypatch):
    reset_storage_service()
    provider = FakeStorageProvider(default_bucket="talos-marketplace")
    service = StorageService(provider=provider, default_bucket="talos-marketplace")
    monkeypatch.setattr("app.storage.service._storage_service_instance", service)
    monkeypatch.setattr("app.routers.marketplace.get_storage_service", lambda: service)
    return provider


@pytest.mark.asyncio
async def test_sdk_client_install_flow(db_session, fake_storage, tmp_path, monkeypatch):
    # Setup test account and listing
    user = Account(account_id=uuid.uuid4(), email=f"sdk_user_{uuid.uuid4().hex[:8]}@talos.dev", role="user")
    db_session.add(user)
    await db_session.flush()

    listing = MarketplaceListing(
        listing_id=uuid.uuid4(),
        author_account_id=user.account_id,
        publisher_slug="sdkauthor",
        author_username="sdkauthor",
        kind="skill",
        slug="calc-skill",
        display_name="Calculator Skill",
        tagline="Performs basic calculations",
        status="approved",
        version="1.0.0",
        install_count=0,
        tags=["skill", "math"],
    )
    db_session.add(listing)
    await db_session.flush()

    # Create zip in storage
    import io, zipfile, hashlib
    skill_yaml = (
        "kind: skill\n"
        "name: calc-skill\n"
        "author: sdkauthor\n"
        "version: 1.0.0\n"
        "display_name: Calculator Skill\n"
        "tagline: Performs basic calculations\n"
        "icon_emoji: 🧮\n"
        "icon_color: '#a3e635'\n"
        "tags: [skill, math]\n"
        "skill_md: SKILL.md\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("skill.yaml", skill_yaml)
        zf.writestr("SKILL.md", "---\nname: calc-skill\ndescription: Math\n---\n# Instructions\n")
        zf.writestr("calc.py", "def calc(): return 42\n")
    zip_bytes = buf.getvalue()
    zip_sha256 = hashlib.sha256(zip_bytes).hexdigest()

    canonical_key = "skills/sdkauthor/calc-skill/1.0.0/package.zip"
    await fake_storage.upload(canonical_key, zip_bytes, "application/zip")

    ver = MarketplacePackageVersion(
        version_id=uuid.uuid4(),
        listing_id=listing.listing_id,
        version="1.0.0",
        storage_key=canonical_key,
        bucket="talos-marketplace",
        file_size=len(zip_bytes),
        sha256=zip_sha256,
        status="published",
    )
    db_session.add(ver)
    await db_session.commit()

    # Auth overrides
    app.dependency_overrides[get_db] = lambda: db_session
    from app.api.v1.marketplace import require_account, get_optional_account
    app.dependency_overrides[require_account] = lambda: user
    app.dependency_overrides[get_optional_account] = lambda: user

    transport = ASGITransport(app=app)

    # Patch TigrisMarketplaceStorage so download_url routes back through ASGI,
    # not to an external Tigris presigned URL that the test transport can't serve.
    from app.infrastructure.storage.tigris import TigrisMarketplaceStorage
    _orig_gen_dl = TigrisMarketplaceStorage.generate_presigned_download_url

    async def _test_gen_dl(self, canonical_key, expires_in=900, filename=None):
        # Return a local ASGI-served URL: the raw download endpoint
        return "http://test/api/v1/marketplace/download/sdkauthor/calc-skill"

    TigrisMarketplaceStorage.generate_presigned_download_url = _test_gen_dl

    # Patch DownloadService to return the zip bytes directly (avoid redirect chain)
    # by patching the v1 download endpoint to stream bytes from FakeStorageProvider.
    from app.services.marketplace.download_service import DownloadService
    _orig_get_dl_url = DownloadService.get_download_url

    async def _test_get_dl_url(self, publisher, slug, version=None):
        return "http://test/_testbytes/sdkauthor/calc-skill"

    DownloadService.get_download_url = _test_get_dl_url

    # Also patch httpx.AsyncClient to intercept the testbytes URL and return raw zip
    orig_async_client = AsyncClient

    def mock_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", mock_async_client)

    # Add a temporary raw-bytes route via a custom transport response
    # by overriding the ASGI app's routing for our sentinel URL.
    # Simplest: patch the DownloadService.get_download_url to return a data-bearing URL
    # that the SDK can fetch as bytes. We use the FakeStorageProvider directly via
    # a custom httpx transport for the download sentinel path.
    import httpx as _httpx

    class _BytesFallbackTransport(_httpx.AsyncBaseTransport):
        """Serves raw zip bytes for our test sentinel URL, ASGI transport for everything else."""
        def __init__(self, asgi_transport, zip_bytes: bytes):
            self._asgi = asgi_transport
            self._zip_bytes = zip_bytes

        async def handle_async_request(self, request: _httpx.Request) -> _httpx.Response:
            if "/_testbytes/" in str(request.url):
                return _httpx.Response(200, content=self._zip_bytes,
                                       headers={"content-type": "application/zip"})
            return await self._asgi.handle_async_request(request)

    combined_transport = _BytesFallbackTransport(transport, zip_bytes)

    def mock_async_client_v2(*args, **kwargs):
        kwargs["transport"] = combined_transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", mock_async_client_v2)

    try:
        sdk = MarketplaceClient(
            cloud_url="http://test",
            backend_url="http://test",
            packages_root=tmp_path / "sdk_packages",
            device_token="mock_device_token",
        )

        meta = await sdk.install("sdkauthor/calc-skill")
        assert meta.slug == "calc-skill"
        assert meta.display_name == "Calculator Skill"
        assert meta.kind == "skill"
        assert meta.author == "sdkauthor"
        assert (tmp_path / "sdk_packages" / "skill" / "sdkauthor" / "calc-skill" / "skill.yaml").exists()

        # Verify cloud install count was incremented
        await db_session.refresh(listing)
        assert listing.install_count == 1
    finally:
        TigrisMarketplaceStorage.generate_presigned_download_url = _orig_gen_dl
        DownloadService.get_download_url = _orig_get_dl_url
        app.dependency_overrides.clear()
