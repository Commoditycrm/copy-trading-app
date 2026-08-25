"""add trailing-stop order type + trail columns

Supports the Sell-All trailing-stop variation: close a position with a native
trailing stop where the broker supports it (Alpaca equities today), else fall
back to a market/limit close. Adds:
  * order_type enum label 'TRAILING_STOP' (UPPERCASE — this column stores the
    Python enum NAME, matching the existing MARKET/LIMIT/STOP/STOP_LIMIT labels;
    the lowercase value is only used in the app, never in the DB)
  * orders.trail_percent  (percent trail, e.g. 5.0000 = 5%)
  * orders.trail_price     (fixed-dollar trail)
Exactly one trail column is set on a TRAILING_STOP order; both NULL otherwise.

Revision ID: e2b7c4f1a9d3
Revises: b1d7f3e9a2c4
Create Date: 2026-08-24 08:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e2b7c4f1a9d3"
down_revision: Union[str, None] = "b1d7f3e9a2c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PG 12+ allows ADD VALUE inside a transaction as long as the new label
    # isn't USED in the same transaction (we only add columns here). The label is
    # the enum NAME (UPPERCASE) — SQLAlchemy's Enum column persists Order.order_type
    # by name, matching MARKET/LIMIT/STOP/STOP_LIMIT already in the type.
    op.execute("ALTER TYPE order_type ADD VALUE IF NOT EXISTS 'TRAILING_STOP'")
    op.add_column("orders", sa.Column("trail_percent", sa.Numeric(9, 4), nullable=True))
    op.add_column("orders", sa.Column("trail_price", sa.Numeric(18, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "trail_price")
    op.drop_column("orders", "trail_percent")
    # Postgres cannot DROP a single enum value; leaving 'TRAILING_STOP' in the
    # order_type enum is harmless (no rows reference it after the columns drop).
