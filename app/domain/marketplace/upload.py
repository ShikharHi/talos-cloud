"""
Talos Cloud — Marketplace Upload Domain Entity & State Machine.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.domain.marketplace.errors import MarketplaceError


class UploadStatus(str, Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    PROMOTING = "promoting"
    PROMOTED = "promoted"
    FAILED = "failed"
    EXPIRED = "expired"


_VALID_UPLOAD_TRANSITIONS = {
    UploadStatus.PENDING: {UploadStatus.UPLOADING, UploadStatus.VERIFYING, UploadStatus.FAILED, UploadStatus.EXPIRED},
    UploadStatus.UPLOADING: {UploadStatus.VERIFYING, UploadStatus.FAILED, UploadStatus.EXPIRED},
    UploadStatus.VERIFYING: {UploadStatus.VERIFIED, UploadStatus.FAILED},
    UploadStatus.VERIFIED: {UploadStatus.PROMOTING, UploadStatus.FAILED},
    UploadStatus.PROMOTING: {UploadStatus.PROMOTED, UploadStatus.FAILED},
    UploadStatus.PROMOTED: set(),
    UploadStatus.FAILED: set(),
    UploadStatus.EXPIRED: set(),
}


@dataclass
class PackageUploadSession:
    upload_id: uuid.UUID
    account_id: uuid.UUID
    listing_id: Optional[uuid.UUID]
    kind: str
    version: str
    staging_key: str
    bucket: str
    expected_size: Optional[int] = None
    sha256: Optional[str] = None
    status: UploadStatus = UploadStatus.PENDING
    failure_reason: Optional[str] = None
    expires_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    promoted_at: Optional[datetime] = None

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        return datetime.now(timezone.utc) > self.expires_at

    def can_transition_to(self, target_status: UploadStatus) -> bool:
        return target_status in _VALID_UPLOAD_TRANSITIONS.get(self.status, set())

    def transition_to(self, target_status: UploadStatus, reason: Optional[str] = None) -> None:
        if not self.can_transition_to(target_status):
            raise MarketplaceError(
                f"Invalid upload state transition from {self.status.value} to {target_status.value}."
            )
        self.status = target_status
        if reason:
            self.failure_reason = reason
        if target_status == UploadStatus.VERIFIED:
            self.verified_at = datetime.now(timezone.utc)
        elif target_status == UploadStatus.PROMOTED:
            self.promoted_at = datetime.now(timezone.utc)
