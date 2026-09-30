"""daily_realized_pnl_snapshots: net_liq (ending account equity) for audit

Stores the ending account equity / net liquidation value on 'marked' snapshot
rows (Webull total_net_liquidation_value, Alpaca equity) for account-value
reconciliation. Audit-only — it does NOT feed the calendar's displayed Day P&L.

Revision ID: d9e2f4a6b8c1
Revises: c8f1a2b3d4e5
"""
import sqlalchemy as sa
from alembic import op

revision = "d9e2f4a6b8c1"
down_revision = "c8f1a2b3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_realized_pnl_snapshots",
        sa.Column("net_liq", sa.Numeric(20, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("daily_realized_pnl_snapshots", "net_liq")
