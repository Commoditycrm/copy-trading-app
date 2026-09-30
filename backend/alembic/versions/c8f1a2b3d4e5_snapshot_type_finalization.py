"""daily_realized_pnl_snapshots: snapshot_type (intraday/eod) finalization flag

Distinguishes a broker MARKED value captured after the session closed (the day's
FINAL figure, safe to show as historical) from one captured mid-session (moving,
must not masquerade as settled P&L). Every existing row defaults to "intraday" —
we can't prove when legacy rows were captured, so they are NOT trusted as
finalized, and the calendar stops presenting them as exact historical broker
P&L (the gaurav Sept 18/21 stale-intraday case). No rows are deleted.

Revision ID: c8f1a2b3d4e5
Revises: b7e1c4a9d2f0
"""
import sqlalchemy as sa
from alembic import op

revision = "c8f1a2b3d4e5"
down_revision = "b7e1c4a9d2f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_realized_pnl_snapshots",
        sa.Column("snapshot_type", sa.String(12), server_default="intraday", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("daily_realized_pnl_snapshots", "snapshot_type")
