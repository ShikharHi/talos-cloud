"""Cloud Identity V2 — Unified Account, Identity, Device, Session, ApiKey, Org, Project Schema.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-27
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 1. Update accounts table if needed
    if "accounts" in existing_tables:
        cols = [c["name"] for c in inspector.get_columns("accounts")]
        if "display_name" not in cols:
            op.add_column("accounts", sa.Column("display_name", sa.String(255), nullable=True))
        if "avatar_url" not in cols:
            op.add_column("accounts", sa.Column("avatar_url", sa.String(500), nullable=True))

    # 2. identities table
    if "identities" not in existing_tables:
        op.create_table(
            "identities",
            sa.Column("identity_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("provider", sa.String(50), nullable=False),
            sa.Column("provider_subject", sa.String(255), nullable=False),
            sa.Column("email", sa.String(255), nullable=False),
            sa.Column("email_verified", sa.Boolean(), server_default="true", nullable=False),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("last_login_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("provider", "provider_subject", name="uq_provider_subject"),
        )
        op.create_index("ix_identities_account_id", "identities", ["account_id"])
        op.create_index("ix_identities_email", "identities", ["email"])

        # Backfill Google identities from existing accounts
        op.execute("""
            INSERT INTO identities (identity_id, account_id, provider, provider_subject, email, email_verified, created_at, last_login_at)
            SELECT gen_random_uuid(), account_id, 'google', google_sub, email, true, created_at, updated_at
            FROM accounts
            WHERE google_sub IS NOT NULL
            ON CONFLICT DO NOTHING
        """)

    # 3. devices table
    if "devices" not in existing_tables:
        op.create_table(
            "devices",
            sa.Column("device_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("device_name", sa.String(255), nullable=False),
            sa.Column("platform", sa.String(50), nullable=False),
            sa.Column("os_version", sa.String(100), nullable=True),
            sa.Column("app_version", sa.String(50), nullable=True),
            sa.Column("device_type", sa.String(50), server_default="desktop", nullable=False),
            sa.Column("status", sa.String(20), server_default="active", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
        )
        op.create_index("ix_devices_account_id", "devices", ["account_id"])
        op.create_index("ix_devices_status", "devices", ["status"])

    # 4. sessions table (unified sessions for web, desktop, cli, remote, service)
    if "sessions" not in existing_tables:
        op.create_table(
            "sessions",
            sa.Column("session_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "device_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("devices.device_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("session_type", sa.String(50), server_default="web", nullable=False),
            sa.Column("refresh_token_hash", sa.String(255), nullable=False),
            sa.Column("ip_address", sa.String(100), nullable=True),
            sa.Column("user_agent", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
        )
        op.create_index("ix_sessions_account_id", "sessions", ["account_id"])
        op.create_index("ix_sessions_device_id", "sessions", ["device_id"])
        op.create_index("ix_sessions_revoked_at", "sessions", ["revoked_at"])
        op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

        # Migrate existing web_sessions into sessions table if web_sessions exists
        if "web_sessions" in existing_tables:
            op.execute("""
                INSERT INTO sessions (session_id, account_id, session_type, refresh_token_hash, user_agent, ip_address, created_at, last_seen_at, expires_at, revoked_at)
                SELECT session_id, account_id, 'web', refresh_token_hash, user_agent, ip_address, created_at, last_used_at, expires_at, revoked_at
                FROM web_sessions
                ON CONFLICT DO NOTHING
            """)

    # 5. api_keys table
    if "api_keys" not in existing_tables:
        op.create_table(
            "api_keys",
            sa.Column("key_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("key_prefix", sa.String(16), nullable=False),
            sa.Column("key_hash", sa.String(255), nullable=False),
            sa.Column("scopes", sa.String(1000), server_default="agent:run,workspace:read", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_api_keys_account_id", "api_keys", ["account_id"])
        op.create_index("ix_api_keys_prefix", "api_keys", ["key_prefix"])

    # 6. organizations table
    if "organizations" not in existing_tables:
        op.create_table(
            "organizations",
            sa.Column("org_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("slug", sa.String(100), unique=True, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # 7. organization_members table
    if "organization_members" not in existing_tables:
        op.create_table(
            "organization_members",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.org_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("role", sa.String(50), server_default="member", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("org_id", "account_id", name="uq_org_member"),
        )

    # 8. projects table
    if "projects" not in existing_tables:
        op.create_table(
            "projects",
            sa.Column("project_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.org_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("slug", sa.String(100), nullable=False),
            sa.Column("execution_mode", sa.String(50), server_default="review", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_projects_slug", "projects", ["slug"])

    # 9. project_members table
    if "project_members" not in existing_tables:
        op.create_table(
            "project_members",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "project_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("role", sa.String(50), server_default="editor", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("project_id", "account_id", name="uq_project_member"),
        )


def downgrade() -> None:
    op.drop_table("project_members")
    op.drop_table("projects")
    op.drop_table("organization_members")
    op.drop_table("organizations")
    op.drop_table("api_keys")
    op.drop_table("sessions")
    op.drop_table("devices")
    op.drop_table("identities")
