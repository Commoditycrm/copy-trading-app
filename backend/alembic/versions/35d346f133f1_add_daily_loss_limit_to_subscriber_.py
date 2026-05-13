"""add daily_loss_limit to subscriber_settings

Revision ID: 35d346f133f1
Revises: b4ff20d653a3
Create Date: 2026-05-12 14:46:01.245134

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '35d346f133f1'
down_revision: Union[str, None] = 'b4ff20d653a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscriber_settings",
        sa.Column("daily_loss_limit", sa.Numeric(precision=20, scale=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriber_settings", "daily_loss_limit")
