"""Billing Engine v3 schema migration

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Wallets table
    op.create_table(
        "wallets",
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("monthly_balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("topup_balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("monthly_grant", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("wallet_id"),
        sa.UniqueConstraint("account_id", name="uq_wallet_account_id"),
    )
    op.create_index(op.f("ix_wallets_account_id"), "wallets", ["account_id"], unique=True)

    # 2. Credit Reservations table
    reservation_status_enum = postgresql.ENUM(
        "HELD", "COMMITTED", "RELEASED", "EXPIRED", name="reservation_status_enum", create_type=False
    )
    reservation_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "credit_reservations",
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.String(length=255), nullable=True),
        sa.Column("amount_reserved", sa.Integer(), nullable=False),
        sa.Column("amount_committed", sa.Integer(), nullable=True),
        sa.Column("status", reservation_status_enum, nullable=False, server_default="HELD"),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.wallet_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("reservation_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_reservation_idempotency_key"),
    )
    op.create_index(op.f("ix_credit_reservations_wallet_id"), "credit_reservations", ["wallet_id"], unique=False)
    op.create_index(op.f("ix_credit_reservations_task_id"), "credit_reservations", ["task_id"], unique=False)
    op.create_index(op.f("ix_credit_reservations_status"), "credit_reservations", ["status"], unique=False)

    # 3. Usage Events table
    unit_type_enum = postgresql.ENUM(
        "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens",
        "per_call", "per_minute", "image_low", "image_medium", "image_high",
        name="unit_type_enum", create_type=False
    )
    unit_type_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "usage_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.String(length=255), nullable=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("capability_id", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=True),
        sa.Column("run_id", sa.String(length=255), nullable=True),
        sa.Column("request_id", sa.String(length=255), nullable=True),
        sa.Column("agent_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="COMPLETED"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("credits_reserved", sa.Numeric(precision=10, scale=4), nullable=False, server_default="0"),
        sa.Column("credits_released", sa.Numeric(precision=10, scale=4), nullable=False, server_default="0"),
        sa.Column("unit_type", unit_type_enum, nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("provider_cost_usd", sa.Numeric(precision=12, scale=8), nullable=True),
        sa.Column("credits_charged", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("event_metadata", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("pricing_version", sa.String(length=50), nullable=False, server_default="v1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(op.f("ix_usage_events_task_id"), "usage_events", ["task_id"], unique=False)
    op.create_index(op.f("ix_usage_events_account_id"), "usage_events", ["account_id"], unique=False)
    op.create_index(op.f("ix_usage_events_capability_id"), "usage_events", ["capability_id"], unique=False)
    op.create_index(op.f("ix_usage_events_created_at"), "usage_events", ["created_at"], unique=False)

    # 4. Capability Pricing table
    op.create_table(
        "capability_pricing",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability_id", sa.String(length=100), nullable=False),
        sa.Column("unit", sa.String(length=50), nullable=False),
        sa.Column("credit_cost", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("target_margin", sa.Float(), nullable=False, server_default="0.75"),
        sa.Column("effective_from", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pricing_version", sa.String(length=50), nullable=False, server_default="v1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_capability_pricing_capability_id"), "capability_pricing", ["capability_id"], unique=False)
    op.create_index(op.f("ix_capability_pricing_active"), "capability_pricing", ["active"], unique=False)

    # 5. Provider Pricing table
    op.create_table(
        "provider_pricing",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("pricing_type", sa.String(length=30), nullable=False, server_default="token"),
        sa.Column("input_cost_usd_per_1m", sa.Numeric(precision=14, scale=8), nullable=True),
        sa.Column("output_cost_usd_per_1m", sa.Numeric(precision=14, scale=8), nullable=True),
        sa.Column("cached_input_cost_usd_per_1m", sa.Numeric(precision=14, scale=8), nullable=True),
        sa.Column("tool_cost_usd", sa.Numeric(precision=14, scale=8), nullable=True),
        sa.Column("image_cost_usd_low", sa.Numeric(precision=14, scale=8), nullable=True),
        sa.Column("image_cost_usd_medium", sa.Numeric(precision=14, scale=8), nullable=True),
        sa.Column("image_cost_usd_high", sa.Numeric(precision=14, scale=8), nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.String(length=100), nullable=False, server_default="v1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_provider_pricing_provider"), "provider_pricing", ["provider"], unique=False)
    op.create_index(op.f("ix_provider_pricing_model_id"), "provider_pricing", ["model_id"], unique=False)
    op.create_index(op.f("ix_provider_pricing_active"), "provider_pricing", ["active"], unique=False)

    # 6. Provider Mapping table
    op.create_table(
        "provider_mapping",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability_id", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("effective_from", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_provider_mapping_capability_id"), "provider_mapping", ["capability_id"], unique=False)
    op.create_index(op.f("ix_provider_mapping_active"), "provider_mapping", ["active"], unique=False)

    # 7. Subscription Plans table
    op.create_table(
        "subscription_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("price_usd", sa.Numeric(precision=10, scale=2), nullable=False, server_default="0"),
        sa.Column("monthly_credits", sa.Integer(), nullable=False),
        sa.Column("reset_period_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("topup_allowed", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("version", sa.String(length=50), nullable=False, server_default="v1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_subscription_plan_name"),
    )
    op.create_index(op.f("ix_subscription_plans_name"), "subscription_plans", ["name"], unique=True)

    # 8. Subscriptions table
    subscription_status_enum = postgresql.ENUM(
        "active", "trialing", "past_due", "cancelled", "expired",
        name="subscription_status_enum", create_type=False
    )
    subscription_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", subscription_status_enum, nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gateway_subscription_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["subscription_plans.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", name="uq_subscription_account_id"),
    )
    op.create_index(op.f("ix_subscriptions_account_id"), "subscriptions", ["account_id"], unique=True)
    op.create_index(op.f("ix_subscriptions_status"), "subscriptions", ["status"], unique=False)
    op.create_index(op.f("ix_subscriptions_next_reset_at"), "subscriptions", ["next_reset_at"], unique=False)

    # 9. Margin Simulations table
    op.create_table(
        "margin_simulations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("pricing_version", sa.String(length=50), nullable=False),
        sa.Column("provider_pricing_version", sa.String(length=100), nullable=False),
        sa.Column("usage_mix", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("simulation_input", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("simulation_output", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("projected_revenue", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("projected_provider_cost", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("projected_variable_cost", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("projected_margin", sa.Float(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["accounts.account_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_margin_simulations_created_at"), "margin_simulations", ["created_at"], unique=False)

    # 10. Task Usage Summary table
    op.create_table(
        "task_usage_summary",
        sa.Column("task_id", sa.String(length=255), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("total_credits_charged", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_cost_usd", sa.Numeric(precision=12, scale=8), nullable=True),
        sa.Column("capability_breakdown", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("pricing_version", sa.String(length=50), nullable=False, server_default="v1"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index(op.f("ix_task_usage_summary_account_id"), "task_usage_summary", ["account_id"], unique=False)
    op.create_index(op.f("ix_task_usage_summary_created_at"), "task_usage_summary", ["created_at"], unique=False)

    # 11. Discard/reset old Account.balance_credits (per Decision 3: start fresh)
    op.execute("UPDATE accounts SET balance_credits = 0")


def downgrade() -> None:
    op.drop_table("task_usage_summary")
    op.drop_table("margin_simulations")
    op.drop_table("subscriptions")
    op.drop_table("subscription_plans")
    op.drop_table("provider_mapping")
    op.drop_table("provider_pricing")
    op.drop_table("capability_pricing")
    op.drop_table("usage_events")
    op.drop_table("credit_reservations")
    op.drop_table("wallets")

    op.execute("DROP TYPE IF EXISTS reservation_status_enum")
    op.execute("DROP TYPE IF EXISTS unit_type_enum")
    op.execute("DROP TYPE IF EXISTS subscription_status_enum")
