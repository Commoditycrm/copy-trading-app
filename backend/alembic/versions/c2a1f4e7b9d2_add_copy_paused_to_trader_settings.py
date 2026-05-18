"""add copy_paused to trader_settings

Revision ID: c2a1f4e7b9d2
Revises: 90a5e705741a
Create Date: 2026-05-15 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2a1f4e7b9d2"
down_revision: Union[str, None] = "90a5e705741a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trader_settings",
        sa.Column(
            "copy_paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("trader_settings", "copy_paused", server_default=None)


def downgrade() -> None:
    op.drop_column("trader_settings", "copy_paused")
