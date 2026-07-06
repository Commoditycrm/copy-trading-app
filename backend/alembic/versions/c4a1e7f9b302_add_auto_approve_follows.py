"""add auto_approve_follows to trader_settings

Per-trader follow policy. False (default, = current behaviour) means a
subscriber must request to follow and the trader approves. True ("auto-allow")
lets any subscriber follow this trader directly, no request/approval needed.

Revision ID: c4a1e7f9b302
Revises: b8d2f4a1c609
Create Date: 2026-07-06 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4a1e7f9b302"
down_revision: Union[str, None] = "b8d2f4a1c609"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trader_settings",
        sa.Column(
            "auto_approve_follows", sa.Boolean(),
            nullable=False, server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("trader_settings", "auto_approve_follows")
