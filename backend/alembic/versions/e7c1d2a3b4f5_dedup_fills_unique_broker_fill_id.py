"""de-duplicate fills and enforce unique broker_fill_id

Revision ID: e7c1d2a3b4f5
Revises: c3b8e1d94f27
Create Date: 2026-09-21 00:00:00.000000

Two sync paths racing to record the same broker fill inserted duplicate rows in
`fills` (same broker_fill_id twice). The extra fill inflated a position's sold/
bought quantity, creating a phantom lot that corrupted the realized-P&L FIFO
cost basis (e.g. a +$48 trade shown as -$113). This removes the duplicate rows
(keeping the earliest) and adds a partial unique index so it can't recur. NULL
broker_fill_ids (synthetic fills) are not affected.
"""
from __future__ import annotations

from alembic import op

revision = "e7c1d2a3b4f5"
down_revision = "c3b8e1d94f27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Delete duplicate fills, keeping the lowest id per broker_fill_id.
    op.execute(
        """
        DELETE FROM fills f
        USING fills keep
        WHERE f.broker_fill_id IS NOT NULL
          AND f.broker_fill_id = keep.broker_fill_id
          AND f.id > keep.id
        """
    )
    # 2) Enforce uniqueness going forward (partial — NULLs allowed to repeat).
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_fills_broker_fill_id
        ON fills (broker_fill_id)
        WHERE broker_fill_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_fills_broker_fill_id")
