"""
Talos Cloud — Account & DeviceToken ORM models.

Design notes:
  - `balance_credits` is a single integer on `Account`, shared in real-time
    across all concurrent tasks for that account. There is NO per-task
    reservation or split. The relay's atomic UPDATE is the sole enforcement
    point (see relay_service.py).
  - `DeviceToken` stores only a bcrypt hash of the raw token. The raw token
    is issued exactly once (at registration) or once per refresh, and is never
    re-derivable from the stored hash. The client holds the raw token in the
    OS keychain (credit_client/keychain.py in the runtime repo).
"""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.ledger import CreditTransaction
    from app.models.wallet import Wallet


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    __tablename__ = "accounts"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # Google's stable subject identifier (sub)
    google_sub: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    # User role: 'user' or 'admin'
    role: Mapped[str] = mapped_column(
        String(20), default="user", nullable=False
    )
    # Account status: 'active', 'suspended', 'deleted'
    status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False
    )
    # DEPRECATED: balance_credits is retained for DB backward compatibility,
    # but Wallet.monthly_balance + Wallet.topup_balance is the sole source of truth.
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



class DeviceToken(Base):
    """
    Device-bound, short-lived auth token.

    IMPORTANT — token storage contract:
      Server side: only `token_hash` (bcrypt) is stored here. The raw token
        is NEVER stored server-side and cannot be re-derived.
      Client side: raw token is stored in the OS keychain (macOS Keychain /
        Windows Credential Manager / Linux Secret Service). It must NEVER
        be stored in a plain file or SQLite table. This is a hard invariant
        motivated by the Cursor CVE: their session tokens lived in an
        unprotected local SQLite file readable by any browser extension.
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
    # bcrypt hash of raw token — NEVER store raw token here
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Optional human-readable label for the device
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
    Browser web session record with rotating refresh token and revocation status.

    IMPORTANT — token storage contract:
      - Raw refresh token is generated with high entropy (e.g. 256 bits).
      - Server-side stores ONLY the SHA-256 hash of the secret portion: `refresh_token_hash`.
      - Access tokens (RS256 JWTs) contain `sid` (session_id) and are short-lived.
      - Revoking a session immediately invalidates both refresh rotation and access validation.
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
