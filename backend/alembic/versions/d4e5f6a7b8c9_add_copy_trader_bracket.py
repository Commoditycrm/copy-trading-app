"""add copy_trader_bracket toggle + percent-distance bracket columns

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-22 00:00:00.000000

Lets a subscriber copy the trader's per-trade SL/TP (re-anchored onto their
own fill) instead of using their own per-position TP/SL %.

  * subscriber_settings.copy_trader_bracket — the opt-in toggle.
  * orders.take_profit_pct / stop_loss_pct — the trader's bracket recorded
    as a percent distance on each mirrored entry, re-anchored at fill time.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriber_settings",
        sa.Column(
            "copy_trader_bracket",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "orders",
        sa.Column("take_profit_pct", sa.Numeric(9, 4), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("stop_loss_pct", sa.Numeric(9, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "stop_loss_pct")
    op.drop_column("orders", "take_profit_pct")
    op.drop_column("subscriber_settings", "copy_trader_bracket")
