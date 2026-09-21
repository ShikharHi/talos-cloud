"""
Unit tests for storage configuration and environment fallbacks.
"""

import os
import pytest
from app.config import Settings
from app.storage.exceptions import StorageConfigurationError
from app.storage.service import get_storage_service, reset_storage_service


def test_storage_config_direct():
    """Tests configuring storage via direct tigris_* properties."""
    s = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        jwt_secret="secret",
        tigris_access_key_id="test-key",
        tigris_secret_access_key="test-secret",
        tigris_endpoint_url="https://t3.storage.dev",
        tigris_region="auto",
        tigris_bucket_name="talos-marketplace",
    )
    assert s.is_storage_configured is True
    assert s.tigris_access_key_id == "test-key"
    assert s.tigris_secret_access_key == "test-secret"
    assert s.tigris_bucket_name == "talos-marketplace"


def test_storage_config_aws_fallbacks(monkeypatch):
    """Tests that standard AWS_* environment variables are resolved as fallbacks."""
    monkeypatch.delenv("TIGRIS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("TIGRIS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "aws-key-123")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-456")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://t3.storage.dev")
    monkeypatch.setenv("AWS_REGION", "auto")

    s = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        jwt_secret="secret",
    )
    assert s.is_storage_configured is True
    assert s.tigris_access_key_id == "aws-key-123"
    assert s.tigris_secret_access_key == "aws-secret-456"
    assert s.tigris_endpoint_url == "https://t3.storage.dev"


def test_storage_config_missing_raises():
    """Tests that accessing get_storage_service without credentials raises StorageConfigurationError."""
    reset_storage_service()
    s = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        jwt_secret="secret",
        tigris_access_key_id=None,
        tigris_secret_access_key=None,
    )
    assert s.is_storage_configured is False
    with pytest.raises(StorageConfigurationError) as exc:
        get_storage_service(s)
    assert "not configured" in str(exc.value)
