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
    # Idempotent adds: on prod/QA these columns were already created out-of-band
    # while alembic stayed at the prior head, so a plain add_column would fail
    # ("column already exists") and block every subsequent migration. IF NOT
    # EXISTS makes this safe on both an already-patched DB (skips) and a fresh
    # one (adds), so the history can finally advance past this revision.
    op.execute(
        "ALTER TABLE subscriber_settings "
        "ADD COLUMN IF NOT EXISTS copy_trader_bracket BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS take_profit_pct NUMERIC(9, 4)")
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS stop_loss_pct NUMERIC(9, 4)")


def downgrade() -> None:
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS stop_loss_pct")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS take_profit_pct")
    op.execute("ALTER TABLE subscriber_settings DROP COLUMN IF EXISTS copy_trader_bracket")
