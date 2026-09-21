"""Production Core Freeze — split reservation columns, upload failure tracking, billing cycle deduplication.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-21
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 1. Wallets: add reserved_monthly and reserved_topup
    if "wallets" in existing_tables:
        cols = [c["name"] for c in inspector.get_columns("wallets")]
        if "reserved_monthly" not in cols:
            op.add_column(
                "wallets",
                sa.Column("reserved_monthly", sa.Integer(), nullable=False, server_default="0"),
            )
        if "reserved_topup" not in cols:
            op.add_column(
                "wallets",
                sa.Column("reserved_topup", sa.Integer(), nullable=False, server_default="0"),
            )

    # 2. Credit Reservations: add monthly_reserved and topup_reserved
    if "credit_reservations" in existing_tables:
        cols = [c["name"] for c in inspector.get_columns("credit_reservations")]
        if "monthly_reserved" not in cols:
            op.add_column(
                "credit_reservations",
                sa.Column("monthly_reserved", sa.Integer(), nullable=False, server_default="0"),
            )
        if "topup_reserved" not in cols:
            op.add_column(
                "credit_reservations",
                sa.Column("topup_reserved", sa.Integer(), nullable=False, server_default="0"),
            )

    # 3. Credit Transactions: add wallet_id, balance_after_monthly, balance_after_topup, action_type
    if "credit_transactions" in existing_tables:
        cols = [c["name"] for c in inspector.get_columns("credit_transactions")]
        if "wallet_id" not in cols:
            op.add_column(
                "credit_transactions",
                sa.Column("wallet_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("wallets.wallet_id", ondelete="SET NULL"), nullable=True),
            )
            op.create_index("ix_credit_transactions_wallet_id", "credit_transactions", ["wallet_id"], if_not_exists=True)
        if "balance_after_monthly" not in cols:
            op.add_column(
                "credit_transactions",
                sa.Column("balance_after_monthly", sa.Integer(), nullable=True),
            )
        if "balance_after_topup" not in cols:
            op.add_column(
                "credit_transactions",
                sa.Column("balance_after_topup", sa.Integer(), nullable=True),
            )
        if "action_type" not in cols:
            op.add_column(
                "credit_transactions",
                sa.Column("action_type", sa.String(50), nullable=True),
            )

    # 4. Package Uploads: add failure_reason, create index on (status, expires_at)
    if "package_uploads" in existing_tables:
        cols = [c["name"] for c in inspector.get_columns("package_uploads")]
        if "failure_reason" not in cols:
            op.add_column(
                "package_uploads",
                sa.Column("failure_reason", sa.Text(), nullable=True),
            )
        op.create_index("ix_package_uploads_status_expires", "package_uploads", ["status", "expires_at"], if_not_exists=True)

    # 5. Subscriptions: add last_grant_id
    if "subscriptions" in existing_tables:
        cols = [c["name"] for c in inspector.get_columns("subscriptions")]
        if "last_grant_id" not in cols:
            op.add_column(
                "subscriptions",
                sa.Column("last_grant_id", sa.String(255), nullable=True),
            )
            op.create_index("ix_subscriptions_last_grant_id", "subscriptions", ["last_grant_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_subscriptions_last_grant_id", table_name="subscriptions")
    op.drop_column("subscriptions", "last_grant_id")
    op.drop_index("ix_package_uploads_status_expires", table_name="package_uploads")
    op.drop_column("package_uploads", "failure_reason")
    op.drop_column("credit_transactions", "action_type")
    op.drop_column("credit_transactions", "balance_after_topup")
    op.drop_column("credit_transactions", "balance_after_monthly")
    op.drop_index("ix_credit_transactions_wallet_id", table_name="credit_transactions")
    op.drop_column("credit_transactions", "wallet_id")
    op.drop_column("credit_reservations", "topup_reserved")
    op.drop_column("credit_reservations", "monthly_reserved")
    op.drop_column("wallets", "reserved_topup")
    op.drop_column("wallets", "reserved_monthly")
