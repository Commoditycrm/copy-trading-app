"""add position_tp_pct + position_sl_pct to subscriber_settings

Per-position TP/SL percentages, applied independently to every open
position the subscriber holds. The pnl_poller hook computes
``unrealized_pnl / abs(cost_basis) * 100`` for every position each tick
and closes any position that breached the configured TP or SL.

Distinct from the daily realized-P&L kill switches: those gate FUTURE
mirror entries; these CLOSE the offending position immediately.
Distinct from auto_liquidation_limit: that's a total-unrealized-USD
ceiling that flattens the WHOLE account; these are per-position.

Revision ID: d8b4c5e7f190
Revises: c7a9d0e2f481
Create Date: 2026-06-10 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8b4c5e7f190"
down_revision: Union[str, None] = "c7a9d0e2f481"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # position_tp_pct uses Numeric(7,2) so a 999.99% TP target (common
    # on options) is representable. position_sl_pct stays at (5,2) — by
    # definition no position can lose more than 100% of cost basis, so
    # an SL value > 100 is meaningless.
    op.add_column(
        "subscriber_settings",
        sa.Column("position_tp_pct", sa.Numeric(7, 2), nullable=True),
    )
    op.add_column(
        "subscriber_settings",
        sa.Column("position_sl_pct", sa.Numeric(5, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriber_settings", "position_sl_pct")
    op.drop_column("subscriber_settings", "position_tp_pct")
