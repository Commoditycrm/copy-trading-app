"""add daily_realized_pnl_snapshots.pct (Alpaca daily return %)

Revision ID: d5c6b7a8e9f0
Revises: e1f2a3b4c5d6
Create Date: 2026-07-27 00:00:00.000000

Adds a nullable ``pct`` column holding the broker-reported daily return %
(Alpaca's portfolio-history profit_loss_pct). NULL for SnapTrade/Webull — that
feed exposes no marked equity, so a matching % can't be computed there. The
Calendar shows the % only where it's present.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d5c6b7a8e9f0"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_realized_pnl_snapshots",
        sa.Column("pct", sa.Numeric(10, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("daily_realized_pnl_snapshots", "pct")
