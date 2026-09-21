"""
Talos Cloud — Marketplace Version Domain Entity.

INVARIANT: Published versions are immutable. Once a version has status=PUBLISHED,
its archive, sha256, and manifest must NEVER be overwritten. Updates require a new version.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class VersionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    TOMBSTONED = "tombstoned"


class SecurityValidationStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FLAGGED = "flagged"
    REJECTED = "rejected"


@dataclass
class PackageVersion:
    version_id: uuid.UUID
    listing_id: uuid.UUID
    version: str
    storage_key: str
    bucket: str
    file_size: int
    sha256: str
    mime_type: str = "application/zip"
    storage_provider: str = "s3"
    manifest_yaml: str = ""
    manifest_json: Optional[dict[str, Any]] = None
    security_report: Optional[dict[str, Any]] = None
    security_status: SecurityValidationStatus = SecurityValidationStatus.PENDING
    status: VersionStatus = VersionStatus.PUBLISHED
    permissions: Optional[dict[str, Any]] = None
    requirements: Optional[dict[str, Any]] = None
    compatibility: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    published_at: Optional[datetime] = None

    @property
    def is_immutable(self) -> bool:
        return self.status == VersionStatus.PUBLISHED
