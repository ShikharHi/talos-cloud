"""Marketplace Production Rebuild — drop package_zip BYTEA, add publisher identity, security reports, reviews, and admin audit.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-21
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 1. Update marketplace_listings
    if "marketplace_listings" in existing_tables:
        cols = [c["name"] for c in inspector.get_columns("marketplace_listings")]

        # Drop legacy BYTEA package_zip column safely
        if "package_zip" in cols:
            op.drop_column("marketplace_listings", "package_zip")

        if "publisher_slug" not in cols:
            op.add_column(
                "marketplace_listings",
                sa.Column("publisher_slug", sa.String(100), nullable=True),
            )
            # Backfill publisher_slug from author_username
            op.execute("UPDATE marketplace_listings SET publisher_slug = author_username WHERE publisher_slug IS NULL")
            op.alter_column("marketplace_listings", "publisher_slug", nullable=False)

        if "description" not in cols:
            op.add_column(
                "marketplace_listings",
                sa.Column("description", sa.Text(), nullable=False, server_default=""),
            )

        if "visibility" not in cols:
            op.add_column(
                "marketplace_listings",
                sa.Column("visibility", sa.String(20), nullable=False, server_default="public"),
            )

        # Unique index on (publisher_slug, slug, kind)
        op.create_index(
            "ix_marketplace_listings_pub_slug_kind",
            "marketplace_listings",
            ["publisher_slug", "slug", "kind"],
            unique=True,
            if_not_exists=True,
        )

    # 2. Update marketplace_package_versions
    if "marketplace_package_versions" in existing_tables:
        cols = [c["name"] for c in inspector.get_columns("marketplace_package_versions")]

        if "manifest_json" not in cols:
            op.add_column(
                "marketplace_package_versions",
                sa.Column("manifest_json", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            )
        if "security_report" not in cols:
            op.add_column(
                "marketplace_package_versions",
                sa.Column("security_report", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            )
        if "security_status" not in cols:
            op.add_column(
                "marketplace_package_versions",
                sa.Column("security_status", sa.String(20), nullable=False, server_default="pending"),
            )
        if "published_at" not in cols:
            op.add_column(
                "marketplace_package_versions",
                sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            )

    # 3. Update package_uploads
    if "package_uploads" in existing_tables:
        cols = [c["name"] for c in inspector.get_columns("package_uploads")]

        if "staging_key" not in cols:
            op.add_column(
                "package_uploads",
                sa.Column("staging_key", sa.String(500), nullable=True),
            )
            op.execute("UPDATE package_uploads SET staging_key = object_key WHERE staging_key IS NULL")
        if "verified_at" not in cols:
            op.add_column(
                "package_uploads",
                sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
            )
        if "promoted_at" not in cols:
            op.add_column(
                "package_uploads",
                sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
            )

    # 4. Update user_installs
    if "user_installs" in existing_tables:
        cols = [c["name"] for c in inspector.get_columns("user_installs")]

        if "version_id" not in cols:
            op.add_column(
                "user_installs",
                sa.Column(
                    "version_id",
                    postgresql.UUID(as_uuid=True),
                    sa.ForeignKey("marketplace_package_versions.version_id", ondelete="SET NULL"),
                    nullable=True,
                ),
            )
        if "install_token" not in cols:
            op.add_column(
                "user_installs",
                sa.Column("install_token", sa.String(128), nullable=True),
            )
            op.create_index("ix_user_installs_install_token", "user_installs", ["install_token"], if_not_exists=True)

    # 5. Create marketplace_reviews
    if "marketplace_reviews" not in existing_tables:
        op.create_table(
            "marketplace_reviews",
            sa.Column("review_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "listing_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("marketplace_listings.listing_id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("rating", sa.SmallInteger(), nullable=False),
            sa.Column("comment", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(20), nullable=False, server_default="published"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("listing_id", "account_id", name="uq_marketplace_review"),
        )

    # 6. Create marketplace_admin_audit
    if "marketplace_admin_audit" not in existing_tables:
        op.create_table(
            "marketplace_admin_audit",
            sa.Column("audit_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "admin_account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "listing_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("marketplace_listings.listing_id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("action", sa.String(50), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("marketplace_admin_audit")
    op.drop_table("marketplace_reviews")
    # Remaining changes intentionally retained for data preservation on downgrade
