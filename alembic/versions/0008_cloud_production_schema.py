"""Cloud Production Schema — missing tables, columns, constraints, and indexes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-20
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 1. Accounts: add status column if missing
    columns_accounts = [c["name"] for c in inspector.get_columns("accounts")]
    if "status" not in columns_accounts:
        op.add_column(
            "accounts",
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        )

    # 1b. Subscription Plans: add budget and limits columns if missing
    if "subscription_plans" in existing_tables:
        columns_plans = [c["name"] for c in inspector.get_columns("subscription_plans")]
        if "internal_usage_budget_usd" not in columns_plans:
            op.add_column(
                "subscription_plans",
                sa.Column("internal_usage_budget_usd", sa.Numeric(10, 2), nullable=False, server_default="5.00"),
            )
        if "max_request_credits" not in columns_plans:
            op.add_column(
                "subscription_plans",
                sa.Column("max_request_credits", sa.Integer(), nullable=False, server_default="10"),
            )
        if "max_run_credits" not in columns_plans:
            op.add_column(
                "subscription_plans",
                sa.Column("max_run_credits", sa.Integer(), nullable=False, server_default="20"),
            )
        if "max_concurrent_requests" not in columns_plans:
            op.add_column(
                "subscription_plans",
                sa.Column("max_concurrent_requests", sa.Integer(), nullable=False, server_default="3"),
            )

    # 2. User Installs: add installed_version, status, updated_at if missing
    if "user_installs" in existing_tables:
        columns_installs = [c["name"] for c in inspector.get_columns("user_installs")]
        if "installed_version" not in columns_installs:
            op.add_column(
                "user_installs",
                sa.Column("installed_version", sa.String(50), nullable=False, server_default="1.0.0"),
            )
        if "status" not in columns_installs:
            op.add_column(
                "user_installs",
                sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            )
        if "updated_at" not in columns_installs:
            op.add_column(
                "user_installs",
                sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            )

    # 3. Marketplace Package Versions: add permissions, requirements, compatibility if missing
    if "marketplace_package_versions" in existing_tables:
        columns_pkg_ver = [c["name"] for c in inspector.get_columns("marketplace_package_versions")]
        if "permissions" not in columns_pkg_ver:
            op.add_column(
                "marketplace_package_versions",
                sa.Column("permissions", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            )
        if "requirements" not in columns_pkg_ver:
            op.add_column(
                "marketplace_package_versions",
                sa.Column("requirements", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            )
        if "compatibility" not in columns_pkg_ver:
            op.add_column(
                "marketplace_package_versions",
                sa.Column("compatibility", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            )

    # 4. Credit Transactions: add reference_id, extra_metadata if missing
    if "credit_transactions" in existing_tables:
        columns_ctx = [c["name"] for c in inspector.get_columns("credit_transactions")]
        if "reference_id" not in columns_ctx:
            op.add_column(
                "credit_transactions",
                sa.Column("reference_id", sa.String(255), nullable=True),
            )
            op.create_index("ix_credit_transactions_reference_id", "credit_transactions", ["reference_id"], if_not_exists=True)
        if "extra_metadata" not in columns_ctx:
            op.add_column(
                "credit_transactions",
                sa.Column("extra_metadata", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            )

    # 5. Billing Transactions table
    if "billing_transactions" not in existing_tables:
        op.create_table(
            "billing_transactions",
            sa.Column("billing_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("gateway", sa.String(50), nullable=False, index=True),
            sa.Column("canonical_reference_id", sa.String(255), nullable=False, index=True),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("amount_minor", sa.BigInteger(), nullable=False),
            sa.Column("currency", sa.String(10), nullable=False),
            sa.Column("credits_granted", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(50), nullable=False),
            sa.Column("payment_type", sa.String(50), nullable=True),
            sa.Column("status", sa.String(30), nullable=False, server_default="processed"),
            sa.Column("metadata_json", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
            sa.UniqueConstraint("gateway", "canonical_reference_id", name="uq_gateway_canonical_ref"),
        )
        op.create_index(
            "ix_billing_transactions_account_created",
            "billing_transactions",
            ["account_id", "created_at"],
            if_not_exists=True,
        )
        op.create_index(
            "ix_billing_transactions_gateway_status",
            "billing_transactions",
            ["gateway", "status"],
            if_not_exists=True,
        )

    # 6. Stripe Customers table
    if "stripe_customers" not in existing_tables:
        op.create_table(
            "stripe_customers",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
                index=True,
            ),
            sa.Column("stripe_customer_id", sa.String(255), unique=True, nullable=False, index=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # 7. Webhook Events table
    if "webhook_events" not in existing_tables:
        op.create_table(
            "webhook_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("provider", sa.String(50), nullable=False, server_default="stripe"),
            sa.Column("event_id", sa.String(255), unique=True, nullable=False, index=True),
            sa.Column("event_type", sa.String(100), nullable=False),
            sa.Column("payload_hash", sa.String(255), nullable=False),
            sa.Column("processed", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # 8. Idempotency Keys table
    if "idempotency_keys" not in existing_tables:
        op.create_table(
            "idempotency_keys",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("key", sa.String(255), nullable=False, index=True),
            sa.Column("request_hash", sa.String(255), nullable=False),
            sa.Column("request_path", sa.String(255), nullable=False),
            sa.Column("status", sa.String(50), nullable=False, server_default="PENDING"),
            sa.Column("response_code", sa.Integer(), nullable=True),
            sa.Column("response_body", sa.Text(), nullable=True),
            sa.Column("locked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )

    # 9. Admin Audit Logs table
    if "admin_audit_logs" not in existing_tables:
        op.create_table(
            "admin_audit_logs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "admin_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="SET NULL"),
                nullable=True,
                index=True,
            ),
            sa.Column("admin_email", sa.String(255), nullable=False),
            sa.Column("action", sa.String(100), nullable=False, index=True),
            sa.Column("resource_type", sa.String(100), nullable=False, index=True),
            sa.Column("resource_id", sa.String(255), nullable=False),
            sa.Column("old_value", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            sa.Column("new_value", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            sa.Column("ip_address", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
        )
        op.create_index(
            "ix_admin_audit_logs_admin_created",
            "admin_audit_logs",
            ["admin_id", "created_at"],
            if_not_exists=True,
        )
        op.create_index(
            "ix_admin_audit_logs_resource_created",
            "admin_audit_logs",
            ["resource_type", "created_at"],
            if_not_exists=True,
        )

    # 10. Pricing Configuration table
    if "pricing_configuration" not in existing_tables:
        op.create_table(
            "pricing_configuration",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("version", sa.String(50), unique=True, nullable=False, index=True),
            sa.Column("credit_reference_usd", sa.Numeric(10, 4), nullable=False, server_default="0.1000"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # 11. Agent Runs table
    if "agent_runs" not in existing_tables:
        op.create_table(
            "agent_runs",
            sa.Column("run_id", sa.String(255), primary_key=True, nullable=False),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("status", sa.String(50), nullable=False, server_default="ACTIVE"),
            sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("total_credits", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # 12. Tool Requests table
    if "tool_requests" not in existing_tables:
        op.create_table(
            "tool_requests",
            sa.Column("request_id", sa.String(255), primary_key=True, nullable=False),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("tool_name", sa.String(100), nullable=False, index=True),
            sa.Column("credits_charged", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(50), nullable=False, server_default="COMPLETED"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("tool_requests")
    op.drop_table("agent_runs")
    op.drop_table("pricing_configuration")
    op.drop_table("admin_audit_logs")
    op.drop_table("idempotency_keys")
    op.drop_table("webhook_events")
    op.drop_table("stripe_customers")
    op.drop_table("billing_transactions")
    op.drop_index("ix_credit_transactions_reference_id", table_name="credit_transactions")
    op.drop_column("credit_transactions", "extra_metadata")
    op.drop_column("credit_transactions", "reference_id")
    op.drop_column("marketplace_package_versions", "compatibility")
    op.drop_column("marketplace_package_versions", "requirements")
    op.drop_column("marketplace_package_versions", "permissions")
    op.drop_column("user_installs", "updated_at")
    op.drop_column("user_installs", "status")
    op.drop_column("user_installs", "installed_version")
    op.drop_column("accounts", "status")
