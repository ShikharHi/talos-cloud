"""Marketplace Unified Economy, Trust, Search Indexing, and Entitlements.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-28
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 1. Update accounts table
    if "accounts" in existing_tables:
        cols = [c["name"] for c in inspector.get_columns("accounts")]
        if "publisher_slug" not in cols:
            op.add_column("accounts", sa.Column("publisher_slug", sa.String(100), nullable=True))
            op.create_index("ix_accounts_publisher_slug", "accounts", ["publisher_slug"], unique=True)
        if "bio" not in cols:
            op.add_column("accounts", sa.Column("bio", sa.Text(), nullable=True))
        if "verified_publisher" not in cols:
            op.add_column("accounts", sa.Column("verified_publisher", sa.Boolean(), server_default="false", nullable=False))

    # 2. Update marketplace_listings table
    if "marketplace_listings" in existing_tables:
        cols = [c["name"] for c in inspector.get_columns("marketplace_listings")]
        if "pricing_type" not in cols:
            op.add_column("marketplace_listings", sa.Column("pricing_type", sa.String(20), server_default="free", nullable=False))
        if "price_credits" not in cols:
            op.add_column("marketplace_listings", sa.Column("price_credits", sa.Integer(), server_default="0", nullable=False))
        if "version_policy" not in cols:
            op.add_column("marketplace_listings", sa.Column("version_policy", sa.String(50), server_default="all_minor_patch", nullable=False))
        if "verified" not in cols:
            op.add_column("marketplace_listings", sa.Column("verified", sa.Boolean(), server_default="false", nullable=False))
        if "download_count" not in cols:
            op.add_column("marketplace_listings", sa.Column("download_count", sa.Integer(), server_default="0", nullable=False))
        if "purchase_count" not in cols:
            op.add_column("marketplace_listings", sa.Column("purchase_count", sa.Integer(), server_default="0", nullable=False))

    # 3. Create marketplace_entitlements table
    if "marketplace_entitlements" not in existing_tables:
        op.create_table(
            "marketplace_entitlements",
            sa.Column("entitlement_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "listing_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("marketplace_listings.listing_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("purchase_id", sa.String(128), unique=True, nullable=False),
            sa.Column("price_paid_credits", sa.Integer(), server_default="0", nullable=False),
            sa.Column("version_policy", sa.String(50), server_default="all_minor_patch", nullable=False),
            sa.Column("status", sa.String(20), server_default="active", nullable=False),
            sa.Column("acquired_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("account_id", "listing_id", name="uq_account_listing_entitlement"),
        )
        op.create_index("ix_marketplace_entitlements_account", "marketplace_entitlements", ["account_id"])
        op.create_index("ix_marketplace_entitlements_listing", "marketplace_entitlements", ["listing_id"])

    # 4. Create creator_earnings table
    if "creator_earnings" not in existing_tables:
        op.create_table(
            "creator_earnings",
            sa.Column("earning_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "creator_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "listing_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("marketplace_listings.listing_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "version_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("marketplace_package_versions.version_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("purchase_id", sa.String(128), nullable=False),
            sa.Column("gross_credits", sa.Integer(), nullable=False),
            sa.Column("platform_share_credits", sa.Integer(), nullable=False),
            sa.Column("creator_share_credits", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), server_default="available", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_creator_earnings_creator", "creator_earnings", ["creator_id"])
        op.create_index("ix_creator_earnings_listing", "creator_earnings", ["listing_id"])
        op.create_index("ix_creator_earnings_purchase", "creator_earnings", ["purchase_id"])


def downgrade() -> None:
    pass
