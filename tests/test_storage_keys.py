"""
Unit tests for centralized object key generation and path traversal defenses.
"""

import pytest
from app.storage.exceptions import StorageValidationError
from app.storage.keys import (
    asset_object_key,
    normalize_resource_type,
    package_object_key,
    sanitize_identifier,
    temp_asset_upload_object_key,
    temp_upload_object_key,
)


def test_valid_package_keys():
    """Tests normal canonical key generation for various package types."""
    assert package_object_key("agent", "researcher", "1.0.0") == "agents/researcher/1.0.0/package.zip"
    assert package_object_key("agents", "data-analysis", "2.1.3") == "agents/data-analysis/2.1.3/package.zip"
    assert package_object_key("skill", "web_search", "0.1.0") == "skills/web_search/0.1.0/package.zip"
    assert package_object_key("mcp", "gmail-connector", "1.0.0") == "mcp/gmail-connector/1.0.0/package.zip"
    assert package_object_key("tool", "calculator", "1.0.0") == "tools/calculator/1.0.0/package.zip"


def test_valid_asset_keys():
    """Tests asset canonical and temp staging key generation."""
    assert asset_object_key("agent", "researcher", "icon.png") == "assets/agents/researcher/icon.png"
    assert asset_object_key("skill", "web_search", "icon.svg") == "assets/skills/web_search/icon.svg"
    assert temp_asset_upload_object_key("agent", "researcher", "up-123", "icon.png") == "uploads/assets/agents/researcher/up-123/icon.png"


def test_valid_temp_upload_key():
    """Tests staging upload key generation."""
    key = temp_upload_object_key("agent", "coder", "upload-uuid-456")
    assert key == "uploads/agents/coder/upload-uuid-456/package.zip"


@pytest.mark.parametrize("bad_id", [
    "../escape",
    "../../etc/passwd",
    "..",
    ".",
    "/root",
    "\\windows\\system32",
    "id\0withnull",
    "id%2e%2e",
    "",
    "   ",
])
def test_path_traversal_rejection(bad_id):
    """Ensures all directory traversal vectors are strictly blocked."""
    with pytest.raises(StorageValidationError):
        sanitize_identifier(bad_id, "identifier")

    with pytest.raises(StorageValidationError):
        package_object_key("agent", bad_id, "1.0.0")

    with pytest.raises(StorageValidationError):
        package_object_key("agent", "valid-id", bad_id)


def test_invalid_resource_type():
    """Ensures unrecognized resource types are rejected."""
    with pytest.raises(StorageValidationError):
        normalize_resource_type("malicious_type")


def test_invalid_asset_extension():
    """Ensures forbidden asset file extensions are rejected."""
    with pytest.raises(StorageValidationError):
        asset_object_key("agent", "researcher", "malicious.exe")

    with pytest.raises(StorageValidationError):
        asset_object_key("agent", "researcher", "script.sh")
