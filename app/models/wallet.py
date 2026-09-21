"""
Talos Cloud — Wallet & CreditReservation ORM models.

Design invariants:
  - One Wallet per Account (1:1, unique FK constraint).
  - monthly_balance: resets every billing cycle; subscription_expiry written at cycle end.
  - topup_balance: NEVER resets; only decremented by usage after monthly credits exhausted.
  - Source of truth: wallet.monthly_balance + wallet.topup_balance must equal
    SUM(credit_transactions.amount) at all times.
  - All mutations go through WalletEngine (wallet_engine.py) which enforces
    row-level locking and atomic SQL. Never mutate directly from outside.

CreditReservation:
  - Created atomically before dispatching to any provider.
  - Status lifecycle: HELD → COMMITTED (actual charge) or RELEASED (failure).
  - EXPIRED: stale HELD reservations cleaned up by background worker.
  - idempotency_key: prevents double-reservation on retry.
"""

import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.accounts import Account


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReservationStatus(str, enum.Enum):
    HELD = "HELD"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class Wallet(Base):
    """
    Per-account credit wallet. One wallet per account (unique FK).

    monthly_balance: subscription credits. Resets on each billing cycle.
    topup_balance: purchased credits. Never expire, never reset.

    Consumption order (enforced by WalletEngine):
      monthly_balance consumed FIRST.
      topup_balance consumed SECOND.

    NEVER mutate these columns directly. Use WalletEngine.reserve() / commit() / release().
    The relay service and all billing operations must go through WalletEngine.
    """
    __tablename__ = "wallets"

    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # enforces 1:1 with Account
        index=True,
    )

    # Subscription credits — reset on each billing cycle
    monthly_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Top-up credits — never reset, never expire
    topup_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # The credit grant given at the last cycle start (used for expiry calculation)
    monthly_grant: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Active reservations holding credits by source
    reserved_monthly: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_topup: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Timestamp of the last monthly reset (used for idempotency in subscription_service)
    reset_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Optimistic locking version counter
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    account: Mapped["Account"] = relationship(back_populates="wallet")
    reservations: Mapped[list["CreditReservation"]] = relationship(
        back_populates="wallet", cascade="all, delete-orphan"
    )

    @property
    def available_monthly(self) -> int:
        """Spendable monthly credits not currently held by any reservation."""
        return max(0, self.monthly_balance - self.reserved_monthly)

    @property
    def available_topup(self) -> int:
        """Spendable topup credits not currently held by any reservation."""
        return max(0, self.topup_balance - self.reserved_topup)

    @property
    def available_credits(self) -> int:
        """Total current spendable credits (unreserved monthly + unreserved topup)."""
        return self.available_monthly + self.available_topup


class CreditReservation(Base):
    """
    Atomic credit hold placed BEFORE dispatching to any provider.

    Status lifecycle:
      HELD      → initial state when pre-check succeeds
      COMMITTED → actual credits charged; remainder refunded to wallet
      RELEASED  → provider failed; full amount returned to wallet
      EXPIRED   → stale HELD reservation cleaned up by background worker

    idempotency_key prevents double-reservation if the relay retries.
    Typical key format: "{task_id}:{capability_id}:{attempt_number}"
    """
    __tablename__ = "credit_reservations"

    reservation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wallets.wallet_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Split sources tracked explicitly
    monthly_reserved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    topup_reserved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Total credits held (monthly_reserved + topup_reserved)
    amount_reserved: Mapped[int] = mapped_column(Integer, nullable=False)
    # How much was actually charged (set when status → COMMITTED)
    amount_committed: Mapped[int | None] = mapped_column(Integer, nullable=True)

    @property
    def actual_amount(self) -> int | None:
        return self.amount_committed

    @actual_amount.setter
    def actual_amount(self, value: int | None) -> None:
        self.amount_committed = value

    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, name="reservation_status_enum"),
        default=ReservationStatus.HELD,
        nullable=False,
        index=True,
    )

    # Prevents duplicate holds on relay retry
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    # Background worker expires stale HELD reservations past this timestamp
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    wallet: Mapped["Wallet"] = relationship(back_populates="reservations")

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_reservation_idempotency_key"),
    )
