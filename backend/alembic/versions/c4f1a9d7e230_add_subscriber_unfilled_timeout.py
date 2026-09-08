"""add per-subscriber unfilled-order auto-cancel timeout

Revision ID: c4f1a9d7e230
Revises: b8d1f4a2c6e9
Create Date: 2026-09-08 00:00:00.000000

Adds an opt-in, per-subscriber timeout that auto-cancels a copied order which
stays WORKING (unfilled) too long, then notifies the subscriber.

  * subscriber_settings.unfilled_timeout_enabled — opt-in toggle (default False).
  * subscriber_settings.unfilled_timeout_seconds  — cancel the mirror once it has
    been working longer than this many seconds (default 60). The UI offers
    seconds/minutes and converts; the column is canonical seconds. Bounds
    (10s..24h) are enforced in the API layer.

Default False preserves existing subscribers' behaviour (no auto-cancel) until
they opt in.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c4f1a9d7e230"
down_revision = "b8d1f4a2c6e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriber_settings",
        sa.Column(
            "unfilled_timeout_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "subscriber_settings",
        sa.Column(
            "unfilled_timeout_seconds",
            sa.Integer(),
            nullable=False,
            server_default="60",
        ),
    )


def downgrade() -> None:
    op.drop_column("subscriber_settings", "unfilled_timeout_seconds")
    op.drop_column("subscriber_settings", "unfilled_timeout_enabled")
