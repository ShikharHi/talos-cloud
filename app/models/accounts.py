"""
Talos Cloud — Unified Cloud Identity ORM models.

Architectural Rule:
  TALOS CLOUD = IDENTITY AUTHORITY
  LOCAL TALOS = EXECUTION CLIENT

Core Hierarchy:
  Account (Cloud user identity)
    ├── Identities (Google, GitHub, Microsoft, Apple, OIDC, etc.)
    ├── Devices (Windows PC, macOS, Linux, etc.)
    │     └── Sessions (Browser, Desktop, CLI, Remote, Service)
    ├── API Keys (Scoped, hashed)
    ├── Organizations & Organization Members
    ├── Projects & Project Members
    └── Agent Runs (Audited execution)
"""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.ledger import CreditTransaction
    from app.models.wallet import Wallet


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    """
    Authoritative user identity owned exclusively by Talos Cloud.
    """
    __tablename__ = "accounts"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    publisher_slug: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True, index=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_publisher: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Legacy google_sub column maintained for backward compatibility with 0002 migration
    google_sub: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    # Role: 'user', 'admin', 'service'
    role: Mapped[str] = mapped_column(
        String(20), default="user", nullable=False, index=True
    )
    # Account status: 'active', 'suspended', 'deleted'
    status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False, index=True
    )
    # DEPRECATED: balance_credits retained for backward compatibility
    balance_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    subscription_tier: Mapped[str] = mapped_column(
        String(50), default="free", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    # Cloud Identity Relationships
    identities: Mapped[list["Identity"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    devices: Mapped[list["Device"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["Session"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    api_keys: Mapped[list["ApiKey"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    project_memberships: Mapped[list["ProjectMember"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    org_memberships: Mapped[list["OrganizationMember"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    # Legacy & Platform Relationships
    device_tokens: Mapped[list["DeviceToken"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    credit_transactions: Mapped[list["CreditTransaction"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    wallet: Mapped["Wallet | None"] = relationship(
        back_populates="account", uselist=False, cascade="all, delete-orphan"
    )
    web_sessions: Mapped[list["WebSessionRecord"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Identity(Base):
    """
    Federated / OAuth identity provider linked to an Account.
    Supports Google, GitHub, Microsoft, Apple, generic OIDC, SAML, etc.
    """
    __tablename__ = "identities"
    __table_args__ = (
        UniqueConstraint("provider", "provider_subject", name="uq_provider_subject"),
        Index("ix_identities_account_id", "account_id"),
        Index("ix_identities_email", "email"),
    )

    identity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # google, github, microsoft, oidc
    provider_subject: Mapped[str] = mapped_column(String(255), nullable=False)  # sub from IdP
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # profile info
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    last_login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    account: Mapped["Account"] = relationship(back_populates="identities")


class Device(Base):
    """
    First-class registered client environment (Desktop, CLI, Server).
    Never an independent user; always attached to a Cloud Account.
    """
    __tablename__ = "devices"
    __table_args__ = (
        Index("ix_devices_account_id", "account_id"),
        Index("ix_devices_status", "status"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    device_name: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)  # windows, darwin, linux
    os_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    device_type: Mapped[str] = mapped_column(String(50), default="desktop", nullable=False)  # desktop, cli, headless, mobile
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active, revoked
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    account: Mapped["Account"] = relationship(back_populates="devices")
    sessions: Mapped[list["Session"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )

    @property
    def is_revoked(self) -> bool:
        return self.status == "revoked" or self.revoked_at is not None


class Session(Base):
    """
    Authoritative authentication session for a client surface (web, desktop, cli, remote, service).
    Stores ONLY SHA-256 / bcrypt hash of refresh token.
    """
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_account_id", "account_id"),
        Index("ix_sessions_device_id", "device_id"),
        Index("ix_sessions_revoked_at", "revoked_at"),
        Index("ix_sessions_expires_at", "expires_at"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.device_id", ondelete="SET NULL"), nullable=True
    )
    session_type: Mapped[str] = mapped_column(String(50), default="web", nullable=False)  # web, desktop, cli, remote, service
    refresh_token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(100), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    account: Mapped["Account"] = relationship(back_populates="sessions")
    device: Mapped["Device | None"] = relationship(back_populates="sessions")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        now = datetime.now(timezone.utc)
        return self.expires_at <= now


class ApiKey(Base):
    """
    First-class scoped Cloud API keys (e.g., talos_sk_live_...).
    Stores only the SHA-256 hash server-side.
    """
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_account_id", "account_id"),
        Index("ix_api_keys_prefix", "key_prefix"),
    )

    key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)  # e.g. talos_sk_live_abc
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)   # SHA-256 hash
    scopes: Mapped[str] = mapped_column(String(1000), default="agent:run,workspace:read", nullable=False)  # comma-separated scopes
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["Account"] = relationship(back_populates="api_keys")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= datetime.now(timezone.utc)


class Organization(Base):
    """
    Organization entity for team/enterprise collaboration.
    """
    __tablename__ = "organizations"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    members: Mapped[list["OrganizationMember"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    projects: Mapped[list["Project"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class OrganizationMember(Base):
    """
    Membership and role inside an Organization.
    """
    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("org_id", "account_id", name="uq_org_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(50), default="member", nullable=False)  # owner, admin, member
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )

    organization: Mapped["Organization"] = relationship(back_populates="members")
    account: Mapped["Account"] = relationship(back_populates="org_memberships")


class Project(Base):
    """
    Project-aware authority container (workspace, skills, MCP, rules, policy).
    """
    __tablename__ = "projects"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    execution_mode: Mapped[str] = mapped_column(
        String(50), default="review", nullable=False
    )  # review, sandbox, trusted, unrestricted
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    organization: Mapped["Organization | None"] = relationship(back_populates="projects")
    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectMember(Base):
    """
    Member role and permissions inside a Project.
    """
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "account_id", name="uq_project_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(50), default="editor", nullable=False)  # owner, admin, editor, viewer
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="members")
    account: Mapped["Account"] = relationship(back_populates="project_memberships")


# ─── Legacy Models Retained for Seamless DB & Migration Compatibility ──────────

class DeviceToken(Base):
    """
    Legacy device-bound token representation.
    Maintained for backward compatibility with 0001 schema and older clients.
    """
    __tablename__ = "device_tokens"
    __table_args__ = (
        Index("ix_device_tokens_lookup", "revoked", "expires_at"),
        Index("ix_device_tokens_account_id", "account_id"),
    )

    token_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    device_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    account: Mapped["Account"] = relationship(back_populates="device_tokens")


class WebSessionRecord(Base):
    """
    Legacy web session record representation from migration 0006.
    Maintained for existing database installations.
    """
    __tablename__ = "web_sessions"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.account_id", ondelete="CASCADE"), nullable=False, index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    account: Mapped["Account"] = relationship(back_populates="web_sessions")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        now = datetime.now(timezone.utc)
        return self.expires_at <= now
