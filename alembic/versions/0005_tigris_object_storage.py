"""Tigris S3-compatible Object Storage Schema

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-20
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. marketplace_package_versions table
    op.create_table(
        "marketplace_package_versions",
        sa.Column("version_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "listing_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("marketplace_listings.listing_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("bucket", sa.String(length=100), nullable=False),
        sa.Column("file_size", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=100), server_default="application/zip", nullable=False),
        sa.Column("storage_provider", sa.String(length=50), server_default="s3", nullable=False),
        sa.Column("manifest_yaml", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="verified", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("listing_id", "version", name="uq_listing_version"),
    )
    op.create_index(
        op.f("ix_marketplace_package_versions_listing_id"),
        "marketplace_package_versions",
        ["listing_id"],
        unique=False,
    )

    # 2. package_uploads table
    op.create_table(
        "package_uploads",
        sa.Column("upload_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "listing_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("marketplace_listings.listing_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resource_type", sa.String(length=20), nullable=False),
        sa.Column("resource_id", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("bucket", sa.String(length=100), nullable=False),
        sa.Column("expected_size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        op.f("ix_package_uploads_account_id"),
        "package_uploads",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_package_uploads_status"),
        "package_uploads",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_package_uploads_status"), table_name="package_uploads")
    op.drop_index(op.f("ix_package_uploads_account_id"), table_name="package_uploads")
    op.drop_table("package_uploads")

    op.drop_index(op.f("ix_marketplace_package_versions_listing_id"), table_name="marketplace_package_versions")
    op.drop_table("marketplace_package_versions")
