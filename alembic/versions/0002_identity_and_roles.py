"""Identity layer — google_sub and role for Account.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("google_sub", sa.String(255), nullable=True))
    op.create_index("ix_accounts_google_sub", "accounts", ["google_sub"], unique=True)
    op.add_column("accounts", sa.Column("role", sa.String(20), nullable=False, server_default="user"))


def downgrade() -> None:
    op.drop_index("ix_accounts_google_sub", table_name="accounts")
    op.drop_column("accounts", "role")
    op.drop_column("accounts", "google_sub")
