"""add eod_unrealized to daily_realized_pnl_snapshots

Stores each account's end-of-day total unrealized P&L (captured by the hourly
snapshot sweep on the current day) so the Calendar can reconstruct MARKED daily
P&L for brokers without a marked-history series (SnapTrade/Webull):

    marked(D) = realized(D) + (eod_unrealized(D) − eod_unrealized(prev day))

Forward-only: NULL on days captured before this shipped, and on Alpaca (which
uses its own portfolio-history marked series).

Revision ID: c8e2f1a4d6b9
Revises: b7d4e1f9a2c3
Create Date: 2026-08-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8e2f1a4d6b9"
down_revision: Union[str, None] = "b7d4e1f9a2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "daily_realized_pnl_snapshots",
        sa.Column("eod_unrealized", sa.Numeric(18, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("daily_realized_pnl_snapshots", "eod_unrealized")
