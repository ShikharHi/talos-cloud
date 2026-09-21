"""
Talos Cloud — Marketplace Listing Domain Entity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class ListingKind(str, Enum):
    AGENT = "agent"
    SKILL = "skill"
    MCP = "mcp"
    TOOL = "tool"


class ListingStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    TOMBSTONED = "tombstoned"


class ListingVisibility(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    UNLISTED = "unlisted"


@dataclass
class Listing:
    listing_id: uuid.UUID
    author_account_id: uuid.UUID
    publisher_slug: str
    author_username: str
    kind: ListingKind
    slug: str
    display_name: str
    tagline: str = ""
    description: str = ""
    icon_emoji: str = "📦"
    icon_color: str = "#a3e635"
    tags: List[str] = field(default_factory=list)
    manifest_yaml: str = ""
    status: ListingStatus = ListingStatus.PENDING_REVIEW
    visibility: ListingVisibility = ListingVisibility.PUBLIC
    version: str = "1.0.0"
    install_count: int = 0
    is_builtin: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def full_slug(self) -> str:
        return f"{self.publisher_slug}/{self.slug}"

    def can_publish_version(self) -> bool:
        return self.status in (ListingStatus.APPROVED, ListingStatus.PENDING_REVIEW)
