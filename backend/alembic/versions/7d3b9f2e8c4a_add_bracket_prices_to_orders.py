"""add bracket prices (take_profit / stop_loss) to orders

Adds two nullable Numeric columns so a trader's entry order can carry an
attached take-profit and stop-loss price. When both are set on a stock
market/limit BUY, the trade endpoint asks Alpaca to place a bracket
order (OrderClass.BRACKET) with attached child legs that fire on fill.

Subscriber mirror orders inherit the same prices (no scaling — price
levels are scale-invariant, multipliers only scale quantity).

Revision ID: 7d3b9f2e8c4a
Revises: d7a8c1b3e9f5
Create Date: 2026-06-04 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7d3b9f2e8c4a'
down_revision: Union[str, None] = 'd7a8c1b3e9f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'orders',
        sa.Column('take_profit_price', sa.Numeric(18, 4), nullable=True),
    )
    op.add_column(
        'orders',
        sa.Column('stop_loss_price', sa.Numeric(18, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('orders', 'stop_loss_price')
    op.drop_column('orders', 'take_profit_price')
