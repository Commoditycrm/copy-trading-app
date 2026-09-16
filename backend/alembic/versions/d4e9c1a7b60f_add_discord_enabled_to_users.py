"""add discord_enabled to users

Per-trader gate for the inbound Discord alert-copying feature. Off by default
(server_default false) so the feature stays hidden until an admin allow-lists a
trader via PATCH /api/admin/users/{id}/discord-enabled. Mirrors sell_all_access.

Revision ID: d4e9c1a7b60f
Revises: f7b2e4c81d93
Create Date: 2026-09-16
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e9c1a7b60f"
down_revision: Union[str, None] = "f7b2e4c81d93"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("discord_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "discord_enabled")
