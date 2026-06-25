"""add retry_max_attempts to subscriber_settings and replace retry_attempted with retry_count on orders

Revision ID: c3d4e5f6a7b8
Revises: 9434aece43f6
Create Date: 2026-06-22 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "9434aece43f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── subscriber_settings ──────────────────────────────────────────────
    op.add_column(
        "subscriber_settings",
        sa.Column(
            "retry_max_attempts",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    # ── orders ───────────────────────────────────────────────────────────
    # Add retry_count (int) — tracks attempts made so far (0 = none yet).
    op.add_column(
        "orders",
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    # Migrate existing data: rows where retry_attempted=True → retry_count=1
    op.execute(
        "UPDATE orders SET retry_count = 1 WHERE retry_attempted = TRUE"
    )
    # Drop the old boolean now that the int column carries the same info.
    op.drop_column("orders", "retry_attempted")


def downgrade() -> None:
    # Restore retry_attempted boolean
    op.add_column(
        "orders",
        sa.Column(
            "retry_attempted",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.execute(
        "UPDATE orders SET retry_attempted = TRUE WHERE retry_count > 0"
    )
    op.drop_column("orders", "retry_count")

    op.drop_column("subscriber_settings", "retry_max_attempts")
