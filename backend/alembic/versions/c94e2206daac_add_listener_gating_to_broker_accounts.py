"""add listener gating to broker_accounts

Revision ID: c94e2206daac
Revises: 5cafb7327ab4
Create Date: 2026-05-30 12:09:25.859119

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c94e2206daac'
down_revision: Union[str, None] = '5cafb7327ab4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add three listener-gating booleans to broker_accounts. server_default
    # 'true' so EXISTING rows inherit the on state (preserves the historic
    # mirror-everything behaviour for already-connected brokers).
    op.add_column("broker_accounts", sa.Column(
        "auto_pull_orders", sa.Boolean(), nullable=False, server_default=sa.text("true")
    ))
    op.add_column("broker_accounts", sa.Column(
        "bring_open_orders", sa.Boolean(), nullable=False, server_default=sa.text("true")
    ))
    op.add_column("broker_accounts", sa.Column(
        "bring_filled_orders", sa.Boolean(), nullable=False, server_default=sa.text("true")
    ))


def downgrade() -> None:
    op.drop_column("broker_accounts", "bring_filled_orders")
    op.drop_column("broker_accounts", "bring_open_orders")
    op.drop_column("broker_accounts", "auto_pull_orders")
