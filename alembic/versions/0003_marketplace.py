"""Marketplace schema

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.create_table(
        "marketplace_listings",
        sa.Column("listing_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_username", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("tagline", sa.String(length=300), nullable=False),
        sa.Column("icon_emoji", sa.String(length=10), nullable=False),
        sa.Column("icon_color", sa.String(length=20), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), server_default='{}', nullable=False),
        sa.Column("manifest_yaml", sa.Text(), nullable=False),
        sa.Column("package_zip", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("install_count", sa.Integer(), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["author_account_id"], ["accounts.account_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("listing_id"),
        sa.UniqueConstraint("author_account_id", "slug", name="uq_listing_author_slug")
    )
    op.create_index(
        op.f("ix_marketplace_listings_author_account_id"),
        "marketplace_listings",
        ["author_account_id"],
        unique=False
    )

    op.create_table(
        "user_installs",
        sa.Column("install_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("listing_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["listing_id"], ["marketplace_listings.listing_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("install_id"),
        sa.UniqueConstraint("account_id", "listing_id", name="uq_user_install")
    )
    op.create_index(
        op.f("ix_user_installs_account_id"),
        "user_installs",
        ["account_id"],
        unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_installs_account_id"), table_name="user_installs")
    op.drop_table("user_installs")
    op.drop_index(op.f("ix_marketplace_listings_author_account_id"), table_name="marketplace_listings")
    op.drop_table("marketplace_listings")
