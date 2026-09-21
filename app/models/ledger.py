"""
Talos Cloud — Credit Ledger & Pricing Event ORM models.

CRITICAL INVARIANT: The `provider` field on `PricingEvent` is INTERNAL ONLY.
It must NEVER appear in any HTTP response body sent to a local client —
not in /relay/* responses, not in /dashboard/* responses, not anywhere.
Tests asserting this are in tests/test_relay.py and tests/test_dashboard.py.
"""

import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.accounts import Account



def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TransactionType(str, enum.Enum):
    """Every type of balance mutation. All mutations write a row here."""
    precheck_debit = "precheck_debit"       # worst-case hold before relay dispatch
    reconcile_refund = "reconcile_refund"   # refund (worst_case - actual) after call
    reconcile_charge = "reconcile_charge"   # charge if actual > worst_case (shouldn't happen but guarded)
    topup = "topup"                          # user purchased top-up credits
    subscription_grant = "subscription_grant"  # cycle-start subscription credit grant
    subscription_expiry = "subscription_expiry"  # unused sub credits expire at cycle end
    relay_spend = "relay_spend"             # committed relay usage
    manual_adjustment = "manual_adjustment" # admin adjustment


class CreditTransaction(Base):
    """
    Append-only ledger of every balance mutation.
    `accounts.balance_credits` must be derivable (auditable) from:
        SELECT SUM(amount) FROM credit_transactions WHERE account_id = :id
    Amounts for debit/expiry transactions are negative; grants/refunds are positive.
    """
    __tablename__ = "credit_transactions"
    __table_args__ = (
        Index("ix_credit_transactions_account_created", "account_id", "created_at"),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    wallet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wallets.wallet_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, name="transaction_type_enum"), nullable=False
    )
    action_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Positive = credit to account, Negative = debit from account.
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    # Balances after this transaction
    balance_after_monthly: Mapped[int | None] = mapped_column(Integer, nullable=True)
    balance_after_topup: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Human-readable note for audit trail (no provider names here)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    extra_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )

    account: Mapped["Account"] = relationship(back_populates="credit_transactions")


class PricingEvent(Base):
    """
    One row per relay-metered provider call. This is the authoritative
    billing record — not client-reported, because the relay IS the metering point.

    INVARIANT: `provider` field is INTERNAL ONLY.
    It must never be included in any HTTP response payload sent to a client.
    The lint/test rule enforcing this is:
        tests/test_relay.py::test_provider_field_never_in_relay_response
        tests/test_dashboard.py::test_provider_field_never_in_dashboard_response
    """
    __tablename__ = "pricing_events"
    __table_args__ = (
        Index("ix_pricing_events_account_created", "account_id", "created_at"),
        Index("ix_pricing_events_cap_created", "capability_id", "created_at"),
        Index("ix_pricing_events_task_acc", "task_id", "account_id"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.account_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Abstract capability ID — safe to expose to clients.
    # e.g. "reasoning_model", "web_search". Never a real provider name.
    capability_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # Real provider name — INTERNAL ONLY. Never send to client.
    # If this field appears in any /relay/* or /dashboard/* response,
    # that is a critical bug. See invariant note at top of file.
    provider: Mapped[str] = mapped_column(String(100), nullable=False)

    # Actual usage measured by the relay (tokens, calls, etc.)
    actual_units: Mapped[int] = mapped_column(BigInteger, default=0)
    # What was pre-checked (conservative worst-case gate)
    precheck_units: Mapped[int] = mapped_column(BigInteger, default=0)
    # Whether the call was rejected before reaching the provider
    rejected: Mapped[bool] = mapped_column(Boolean, default=False)

    real_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    credits_charged: Mapped[int] = mapped_column(Integer, default=0)

    # Which pricing schedule was active when this call was billed.
    # Historical charges remain auditable even after pricing_version changes.
    pricing_version: Mapped[str] = mapped_column(String(50), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )
