"""add per-subscriber EOD 0DTE auto-close settings

Revision ID: e1f2a3b4c5d6
Revises: b7d4e2f1a9c3
Create Date: 2026-07-27 00:00:00.000000

Makes the end-of-day same-day-expiry (0DTE) option auto-close a per-subscriber,
opt-in feature instead of a global fixed 15-minute window.

  * subscriber_settings.eod_autoclose_enabled — opt-in toggle (default False).
  * subscriber_settings.eod_autoclose_minutes  — how many minutes before the
    16:00 ET close to flatten 0DTE options + refuse new 0DTE mirrors (1..30,
    default 15). Range is enforced in the API + market_hours.clamp_eod_minutes.

Default False preserves existing subscribers' behaviour (no auto-close) until
they opt in.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "e1f2a3b4c5d6"
down_revision = "b7d4e2f1a9c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriber_settings",
        sa.Column(
            "eod_autoclose_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "subscriber_settings",
        sa.Column(
            "eod_autoclose_minutes",
            sa.Integer(),
            nullable=False,
            server_default="15",
        ),
    )


def downgrade() -> None:
    op.drop_column("subscriber_settings", "eod_autoclose_minutes")
    op.drop_column("subscriber_settings", "eod_autoclose_enabled")
