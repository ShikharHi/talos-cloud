"""
Talos Cloud — Production Marketplace ORM models.

Tables:
  marketplace_listings         — published packages (agents, tools, mcp, skills)
  marketplace_package_versions — immutable release versions stored in Tigris S3
  package_uploads              — staging upload sessions and state transitions
  user_installs                — verified installation records and active tokens
  marketplace_reviews          — user ratings and reviews
  marketplace_admin_audit      — moderation action audit trail

INVARIANT: No binary packages are stored in PostgreSQL. All package archives
reside in Tigris object storage.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer,
    SmallInteger, String, Text, func, UniqueConstraint, JSON
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.accounts import Account


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarketplaceListing(Base):
    __tablename__ = "marketplace_listings"
    __table_args__ = (
        UniqueConstraint("publisher_slug", "slug", "kind", name="uq_listing_pub_slug_kind"),
        UniqueConstraint("author_account_id", "slug", name="uq_listing_author_slug"),
        Index("ix_marketplace_listings_status_kind", "status", "kind"),
        Index("ix_marketplace_listings_status_created", "status", "created_at"),
        Index("ix_marketplace_listings_install_count", "install_count"),
        Index("ix_marketplace_listings_pub_slug", "publisher_slug"),
    )

    listing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    author_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Immutable public publisher namespace slug
    publisher_slug: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # Legacy author username (for backwards compat)
    author_username: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # Package kind: agent | tool | mcp | skill
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    tagline: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    icon_emoji: Mapped[str] = mapped_column(String(10), nullable=False, default="📦")
    icon_color: Mapped[str] = mapped_column(String(20), nullable=False, default="#a3e635")
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String).with_variant(JSON(), 'sqlite'), nullable=False, server_default="{}"
    )
    # Canonical manifest YAML text for quick indexing and detail retrieval
    manifest_yaml: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Status: pending_review | approved | rejected | tombstoned
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending_review")
    # Visibility: public | private | unlisted
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, default="public")
    # Cached latest version string
    version: Mapped[str] = mapped_column(String(50), nullable=False, default="1.0.0")
    # Cached install count derived from active UserInstall rows
    install_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    author: Mapped["Account"] = relationship("Account", foreign_keys=[author_account_id])
    installs: Mapped[list["UserInstall"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )
    package_versions: Mapped[list["MarketplacePackageVersion"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan", order_by="desc(MarketplacePackageVersion.created_at)"
    )
    reviews: Mapped[list["MarketplaceReview"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )

    @property
    def full_slug(self) -> str:
        pub = self.publisher_slug or self.author_username
        return f"{pub}/{self.slug}"

    def __init__(self, **kwargs: Any) -> None:
        if "publisher_slug" not in kwargs or not kwargs["publisher_slug"]:
            kwargs["publisher_slug"] = kwargs.get("author_username") or "talos"
        if "author_username" not in kwargs or not kwargs["author_username"]:
            kwargs["author_username"] = kwargs.get("publisher_slug") or "talos"
        if "tags" not in kwargs or not isinstance(kwargs.get("tags"), list):
            kwargs["tags"] = []
        super().__init__(**kwargs)


class MarketplacePackageVersion(Base):
    """
    Represents an immutable, published version of a marketplace package in Tigris S3.
    """
    __tablename__ = "marketplace_package_versions"
    __table_args__ = (
        UniqueConstraint("listing_id", "version", name="uq_listing_version"),
        Index("ix_marketplace_pkg_versions_status", "status"),
    )

    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("marketplace_listings.listing_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False, default="application/zip")
    storage_provider: Mapped[str] = mapped_column(String(50), nullable=False, default="s3")
    manifest_yaml: Mapped[str] = mapped_column(Text, nullable=False, default="")
    manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Security scanner output
    security_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Security status: pending | passed | flagged | rejected
    security_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # Metadata specifications
    permissions: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    requirements: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    compatibility: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Lifecycle status: draft | published | deprecated | tombstoned
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="published")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    listing: Mapped["MarketplaceListing"] = relationship(back_populates="package_versions")


class PackageUpload(Base):
    """
    Tracks an in-flight upload session from client initiation to verification & promotion.
    """
    __tablename__ = "package_uploads"
    __table_args__ = (
        Index("ix_package_uploads_status_expires", "status", "expires_at"),
    )

    upload_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    listing_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("marketplace_listings.listing_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    resource_type: Mapped[str] = mapped_column(String(20), nullable=False)  # agent, skill, mcp, tool
    resource_id: Mapped[str] = mapped_column(String(100), nullable=False)   # slug or uuid
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)    # legacy alias to staging_key
    staging_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    expected_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # State machine: pending -> uploading -> verifying -> verified -> promoting -> promoted / failed / expired
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserInstall(Base):
    """
    Tracks verified installations by account, with exact version and confirmation token.
    """
    __tablename__ = "user_installs"
    __table_args__ = (
        UniqueConstraint("account_id", "listing_id", name="uq_user_install"),
        Index("ix_user_installs_listing_id", "listing_id"),
        Index("ix_user_installs_token", "install_token"),
    )

    install_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("marketplace_listings.listing_id", ondelete="CASCADE"),
        nullable=False,
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("marketplace_package_versions.version_id", ondelete="SET NULL"),
        nullable=True,
    )
    installed_version: Mapped[str] = mapped_column(String(50), nullable=False)
    # States: pending_download | active | removed | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    install_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    listing: Mapped["MarketplaceListing"] = relationship(back_populates="installs")


class MarketplaceReview(Base):
    """
    User reviews and ratings for marketplace listings.
    """
    __tablename__ = "marketplace_reviews"
    __table_args__ = (
        UniqueConstraint("listing_id", "account_id", name="uq_marketplace_review"),
        Index("ix_marketplace_reviews_listing_id", "listing_id"),
    )

    review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("marketplace_listings.listing_id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="CASCADE"),
        nullable=False,
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1-5
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="published")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    listing: Mapped["MarketplaceListing"] = relationship(back_populates="reviews")


class MarketplaceAdminAudit(Base):
    """
    Audit log of moderation actions performed on listings.
    """
    __tablename__ = "marketplace_admin_audit"
    __table_args__ = (
        Index("ix_marketplace_audit_listing_id", "listing_id"),
    )

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    admin_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="CASCADE"),
        nullable=False,
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("marketplace_listings.listing_id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
