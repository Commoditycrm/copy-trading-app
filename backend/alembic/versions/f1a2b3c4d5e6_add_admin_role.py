"""add 'admin' value to user_role enum

Adds a third role — ADMIN — for platform operators. Admin users have
access to /api/admin/* endpoints and the /admin frontend panel.
They are NOT created through normal registration; use
scripts/create_admin.py to promote or seed the first admin account.

Revision ID: f1a2b3c4d5e6
Revises: c8d3f5a92e14
Create Date: 2026-05-26 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "c8d3f5a92e14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction — autocommit_block
    # is required even on Postgres 12+ when Alembic wraps migrations in a txn.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'admin'")


def downgrade() -> None:
    # Postgres cannot drop an enum value without recreating the type and
    # re-casting every column. Since admin rows are rare and operator-managed,
    # we accept the residual value on downgrade rather than do the destructive
    # dance. Manually delete admin users before rolling back if needed.
    pass
