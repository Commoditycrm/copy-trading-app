"""add last_activity_sync_at

Revision ID: a838e919b693
Revises: 35d346f133f1
Create Date: 2026-05-13 14:13:14.075465

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a838e919b693'
down_revision: Union[str, None] = '35d346f133f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broker_accounts",
        sa.Column("last_activity_sync_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("broker_accounts", "last_activity_sync_at")
