"""Initial schema — all tables for Talos Cloud credit & billing system.

Revision ID: 0001
Revises: None
Create Date: 2026-08-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # accounts
    op.create_table(
        "accounts",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("balance_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("subscription_tier", sa.String(50), nullable=False, server_default="free"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("account_id"),
        sa.UniqueConstraint("email"),
    )

    # device_tokens
    op.create_table(
        "device_tokens",
        sa.Column("token_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("device_label", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("token_id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_device_tokens_account_id", "device_tokens", ["account_id"])

    # transaction type enum
    transaction_type_enum = postgresql.ENUM(
        "precheck_debit", "reconcile_refund", "reconcile_charge",
        "topup", "subscription_grant", "subscription_expiry",
        name="transaction_type_enum",
        create_type=False,
    )
    transaction_type_enum.create(op.get_bind(), checkfirst=True)

    # credit_transactions
    op.create_table(
        "credit_transactions",
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.String(255), nullable=True),
        sa.Column("type", transaction_type_enum, nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("transaction_id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_credit_transactions_account_id", "credit_transactions", ["account_id"])
    op.create_index("ix_credit_transactions_task_id", "credit_transactions", ["task_id"])
    op.create_index("ix_credit_transactions_created_at", "credit_transactions", ["created_at"])

    # pricing_versions
    op.create_table(
        "pricing_versions",
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("schedule_yaml", sa.Text(), nullable=False),
        sa.Column("published_by", sa.String(255), nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("version_id"),
        sa.UniqueConstraint("version"),
    )

    # pricing_events — one row per relay-metered provider call
    op.create_table(
        "pricing_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", sa.String(255), nullable=True),
        sa.Column("capability_id", sa.String(100), nullable=False),
        # INTERNAL ONLY — never in API responses. See PricingEvent model docstring.
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("actual_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("precheck_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("rejected", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("real_cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("credits_charged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pricing_version", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("event_id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"], ondelete="SET NULL"),
    )
    op.create_index("ix_pricing_events_account_id", "pricing_events", ["account_id"])
    op.create_index("ix_pricing_events_task_id", "pricing_events", ["task_id"])
    op.create_index("ix_pricing_events_created_at", "pricing_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("pricing_events")
    op.drop_table("pricing_versions")
    op.drop_table("credit_transactions")
    op.execute("DROP TYPE IF EXISTS transaction_type_enum")
    op.drop_table("device_tokens")
    op.drop_table("accounts")
