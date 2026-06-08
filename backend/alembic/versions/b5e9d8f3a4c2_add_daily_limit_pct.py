"""add daily_loss_limit_pct + daily_profit_limit_pct to subscriber_settings

Subscriber-side risk limits move from absolute USD values to percentages
of the day-start broker balance. The pnl_poller computes the dollar
threshold each tick as ``beginning_day_balance * pct / 100`` and trips
the same kill switch when today's realized P&L breaches it. This keeps
the limit scale-correct as the account size changes (a $500 limit is
loose on a $200K account and tight on a $5K account; a 5% limit is
consistently meaningful at any size).

Both columns are nullable Numeric(5,2) — a percent between 0 and 100
with 2 decimal places (e.g. 7.50). The legacy USD columns
``daily_loss_limit`` / ``daily_profit_limit`` are kept for backward
compatibility but the UI no longer exposes them.

Revision ID: b5e9d8f3a4c2
Revises: 9a4c2e7b8d5f
Create Date: 2026-06-04 19:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5e9d8f3a4c2'
down_revision: Union[str, None] = '9a4c2e7b8d5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'subscriber_settings',
        sa.Column('daily_loss_limit_pct', sa.Numeric(5, 2), nullable=True),
    )
    op.add_column(
        'subscriber_settings',
        sa.Column('daily_profit_limit_pct', sa.Numeric(5, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('subscriber_settings', 'daily_profit_limit_pct')
    op.drop_column('subscriber_settings', 'daily_loss_limit_pct')
