"""add percentage-based risk limits to subscriber_settings

Adds three new risk controls (all nullable — NULL = feature disabled):
  - daily_loss_limit_pct   : stop copying if today's realized loss exceeds X% of account
  - per_trade_loss_limit_pct : stop copying if any single trade loses more than X% of account
  - max_drawdown_pct         : stop copying if account equity drops X% below the baseline
  - max_drawdown_equity_baseline : account equity captured when max_drawdown protection is set

The legacy dollar-amount daily_loss_limit column is kept for backward
compatibility — existing subscribers who had a dollar limit still work.
New UI uses the pct fields only.

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-05-27 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscriber_settings",
        sa.Column("daily_loss_limit_pct", sa.Numeric(precision=6, scale=3), nullable=True),
    )
    op.add_column(
        "subscriber_settings",
        sa.Column("per_trade_loss_limit_pct", sa.Numeric(precision=6, scale=3), nullable=True),
    )
    op.add_column(
        "subscriber_settings",
        sa.Column("max_drawdown_pct", sa.Numeric(precision=6, scale=3), nullable=True),
    )
    op.add_column(
        "subscriber_settings",
        sa.Column("max_drawdown_equity_baseline", sa.Numeric(precision=20, scale=4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriber_settings", "max_drawdown_equity_baseline")
    op.drop_column("subscriber_settings", "max_drawdown_pct")
    op.drop_column("subscriber_settings", "per_trade_loss_limit_pct")
    op.drop_column("subscriber_settings", "daily_loss_limit_pct")
