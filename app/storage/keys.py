"""
Talos Cloud — Centralized Object Key Scheme.

Centralizes key generation and strictly enforces path traversal protection.
No client-supplied paths are ever trusted.

Key scheme:
  - Packages:
      agents/{agent_id}/{version}/package.zip
      skills/{skill_id}/{version}/package.zip
      mcp/{mcp_id}/{version}/package.zip
      tools/{tool_id}/{version}/package.zip

  - Assets:
      assets/agents/{agent_id}/icon.png
      assets/skills/{skill_id}/icon.png
      assets/mcp/{mcp_id}/icon.png
      assets/tools/{tool_id}/icon.png

  - In-flight / temporary uploads:
      uploads/{resource_type}/{resource_id}/{upload_id}/package.zip
"""

import re
from typing import Literal
from app.storage.exceptions import StorageValidationError

# Strict identifier regex: alphanumeric, dash, underscore, dot.
# Must NOT start with dot or dash, cannot contain path traversal.
IDENTIFIER_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-\.]{0,99}$")

ALLOWED_RESOURCE_TYPES = {
    "agent": "agents",
    "agents": "agents",
    "skill": "skills",
    "skills": "skills",
    "mcp": "mcp",
    "tool": "tools",
    "tools": "tools",
}

ALLOWED_ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".svg", ".webp"}


def normalize_resource_type(resource_type: str) -> str:
    """Normalize resource type to canonical plural form."""
    rt = resource_type.strip().lower()
    if rt not in ALLOWED_RESOURCE_TYPES:
        raise StorageValidationError(
            f"Invalid resource type '{resource_type}'. "
            f"Allowed types: {sorted(list(set(ALLOWED_RESOURCE_TYPES.values())))}"
        )
    return ALLOWED_RESOURCE_TYPES[rt]


def sanitize_identifier(identifier: str, field_name: str = "identifier") -> str:
    """
    Validates that an identifier (resource_id, slug, version, upload_id)
    is safe from directory traversal and injection.
    """
    if not identifier or not isinstance(identifier, str):
        raise StorageValidationError(f"Invalid {field_name}: cannot be empty")

    clean = identifier.strip()

    # Disallow path separators, null bytes, backslashes
    if "/" in clean or "\\" in clean or "\0" in clean or "%" in clean:
        raise StorageValidationError(f"Invalid {field_name} '{clean}': contains forbidden path characters")

    # Disallow relative path dots
    if clean in (".", "..") or ".." in clean:
        raise StorageValidationError(f"Invalid {field_name} '{clean}': path traversal detected")

    if not IDENTIFIER_REGEX.match(clean):
        raise StorageValidationError(
            f"Invalid {field_name} '{clean}': must match alphanumeric, hyphen, underscore, or dot characters"
        )

    return clean


def package_object_key(resource_type: str, resource_id: str, version: str) -> str:
    """
    Generates canonical permanent object key for published package versions:
    e.g. agents/{agent_id}/{version}/package.zip
    """
    norm_type = normalize_resource_type(resource_type)
    clean_id = sanitize_identifier(resource_id, "resource_id")
    clean_version = sanitize_identifier(version, "version")
    return f"{norm_type}/{clean_id}/{clean_version}/package.zip"


def temp_upload_object_key(resource_type: str, resource_id: str, upload_id: str) -> str:
    """
    Generates temporary staging object key for in-flight uploads:
    e.g. uploads/{resource_type}/{resource_id}/{upload_id}/package.zip
    """
    norm_type = normalize_resource_type(resource_type)
    clean_id = sanitize_identifier(resource_id, "resource_id")
    clean_upload_id = sanitize_identifier(upload_id, "upload_id")
    return f"uploads/{norm_type}/{clean_id}/{clean_upload_id}/package.zip"


def asset_object_key(resource_type: str, resource_id: str, filename: str = "icon.png") -> str:
    """
    Generates canonical asset object key:
    e.g. assets/agents/{agent_id}/icon.png
    """
    norm_type = normalize_resource_type(resource_type)
    clean_id = sanitize_identifier(resource_id, "resource_id")
    clean_filename = sanitize_identifier(filename, "filename")

    # Ensure valid asset extension
    ext = "." + clean_filename.split(".")[-1].lower() if "." in clean_filename else ""
    if ext not in ALLOWED_ASSET_EXTENSIONS:
        raise StorageValidationError(
            f"Invalid asset filename '{filename}'. Permitted extensions: {sorted(list(ALLOWED_ASSET_EXTENSIONS))}"
        )

    return f"assets/{norm_type}/{clean_id}/{clean_filename}"


def temp_asset_upload_object_key(resource_type: str, resource_id: str, upload_id: str, filename: str = "icon.png") -> str:
    """
    Generates temporary staging key for asset uploads:
    e.g. uploads/assets/{resource_type}/{resource_id}/{upload_id}/{filename}
    """
    norm_type = normalize_resource_type(resource_type)
    clean_id = sanitize_identifier(resource_id, "resource_id")
    clean_upload_id = sanitize_identifier(upload_id, "upload_id")
    clean_filename = sanitize_identifier(filename, "filename")
    return f"uploads/assets/{norm_type}/{clean_id}/{clean_upload_id}/{clean_filename}"
