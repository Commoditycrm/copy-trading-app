"""add copy_multiplier to orders

Revision ID: f2b8c1d4e7a9
Revises: b1d7f3e9a2c4
Create Date: 2026-09-01 00:00:00.000000

Records the subscriber's copy-size multiplier used to scale each mirror order,
so a later close can reference the ENTRY multiplier even if the subscriber
changes it mid-position. NULL for trader/standalone orders.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2b8c1d4e7a9"
down_revision: Union[str, None] = "b1d7f3e9a2c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("copy_multiplier", sa.Numeric(9, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "copy_multiplier")
