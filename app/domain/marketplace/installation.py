"""
Talos Cloud — Marketplace Installation Domain Entity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class InstallStatus(str, Enum):
    PENDING_DOWNLOAD = "pending_download"
    ACTIVE = "active"
    REMOVED = "removed"
    FAILED = "failed"


@dataclass
class InstallationRecord:
    install_id: uuid.UUID
    account_id: uuid.UUID
    listing_id: uuid.UUID
    version_id: Optional[uuid.UUID]
    installed_version: str
    status: InstallStatus = InstallStatus.ACTIVE
    install_token: Optional[str] = None
    installed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def is_active(self) -> bool:
        return self.status == InstallStatus.ACTIVE
