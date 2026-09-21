"""Performance and composite indexes for high-throughput query patterns

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-20
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. usage_events composite and query indexes
    op.create_index(
        "ix_usage_events_account_created",
        "usage_events",
        ["account_id", "created_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_usage_events_status_created",
        "usage_events",
        ["status", "created_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_usage_events_account_status",
        "usage_events",
        ["account_id", "status"],
        if_not_exists=True,
    )

    # 2. credit_transactions composite index for wallet ledger auditing
    op.create_index(
        "ix_credit_transactions_account_created",
        "credit_transactions",
        ["account_id", "created_at"],
        if_not_exists=True,
    )

    # 3. pricing_events composite index for usage breakdown and auditing
    op.create_index(
        "ix_pricing_events_account_created",
        "pricing_events",
        ["account_id", "created_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_pricing_events_cap_created",
        "pricing_events",
        ["capability_id", "created_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_pricing_events_task_acc",
        "pricing_events",
        ["task_id", "account_id"],
        if_not_exists=True,
    )

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 4. billing_transactions composite indexes for payment reconciliation
    if "billing_transactions" in existing_tables:
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

    # 5. admin_audit_logs composite indexes for security investigations
    if "admin_audit_logs" in existing_tables:
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

    # 6. marketplace_listings search and ranking indexes
    op.create_index(
        "ix_marketplace_listings_status_kind",
        "marketplace_listings",
        ["status", "kind"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_marketplace_listings_status_created",
        "marketplace_listings",
        ["status", "created_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_marketplace_listings_install_count",
        "marketplace_listings",
        ["install_count"],
        if_not_exists=True,
    )

    # 7. user_installs foreign key index for cascade/lookup optimization
    op.create_index(
        "ix_user_installs_listing_id",
        "user_installs",
        ["listing_id"],
        if_not_exists=True,
    )

    # 8. device_tokens active token lookup index
    op.create_index(
        "ix_device_tokens_lookup",
        "device_tokens",
        ["revoked", "expires_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    op.drop_index("ix_device_tokens_lookup", table_name="device_tokens", if_exists=True)
    op.drop_index("ix_user_installs_listing_id", table_name="user_installs", if_exists=True)
    op.drop_index("ix_marketplace_listings_install_count", table_name="marketplace_listings", if_exists=True)
    op.drop_index("ix_marketplace_listings_status_created", table_name="marketplace_listings", if_exists=True)
    op.drop_index("ix_marketplace_listings_status_kind", table_name="marketplace_listings", if_exists=True)
    if "admin_audit_logs" in existing_tables:
        op.drop_index("ix_admin_audit_logs_resource_created", table_name="admin_audit_logs", if_exists=True)
        op.drop_index("ix_admin_audit_logs_admin_created", table_name="admin_audit_logs", if_exists=True)
    if "billing_transactions" in existing_tables:
        op.drop_index("ix_billing_transactions_gateway_status", table_name="billing_transactions", if_exists=True)
        op.drop_index("ix_billing_transactions_account_created", table_name="billing_transactions", if_exists=True)
    op.drop_index("ix_pricing_events_task_acc", table_name="pricing_events", if_exists=True)
    op.drop_index("ix_pricing_events_cap_created", table_name="pricing_events", if_exists=True)
    op.drop_index("ix_pricing_events_account_created", table_name="pricing_events", if_exists=True)
    op.drop_index("ix_credit_transactions_account_created", table_name="credit_transactions", if_exists=True)
    op.drop_index("ix_usage_events_account_status", table_name="usage_events", if_exists=True)
    op.drop_index("ix_usage_events_status_created", table_name="usage_events", if_exists=True)
    op.drop_index("ix_usage_events_account_created", table_name="usage_events", if_exists=True)
