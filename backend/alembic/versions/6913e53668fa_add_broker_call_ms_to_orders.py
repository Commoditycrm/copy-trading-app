"""add broker_call_ms to orders

Revision ID: 6913e53668fa
Revises: f3c9d7b1e042
Create Date: 2026-05-28 19:26:42.123486

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '6913e53668fa'
down_revision: Union[str, None] = 'f3c9d7b1e042'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("broker_call_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "broker_call_ms")
