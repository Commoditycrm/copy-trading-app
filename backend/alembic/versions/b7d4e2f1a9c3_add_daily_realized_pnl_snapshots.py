"""add daily_realized_pnl_snapshots

Durable per-user, per-day realized-P&L table the Calendar reads
(see models/daily_realized_pnl_snapshot.py). Single-parent migration off the
current head 6f2b9c4e1a70 — the DB is already there, so `alembic upgrade head`
applies only this.

Revision ID: b7d4e2f1a9c3
Revises: 6f2b9c4e1a70
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "b7d4e2f1a9c3"
down_revision = "6f2b9c4e1a70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_realized_pnl_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(18, 2), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("broker_account_id", UUID(as_uuid=True), sa.ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("broker", sa.String(40), nullable=True),
        sa.Column("source", sa.String(24), nullable=False, server_default="broker_activities"),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "day", name="uq_daily_realized_pnl_user_day"),
    )
    op.create_index("ix_daily_realized_pnl_user_id", "daily_realized_pnl_snapshots", ["user_id"])
    op.create_index("ix_daily_realized_pnl_day", "daily_realized_pnl_snapshots", ["day"])


def downgrade() -> None:
    op.drop_index("ix_daily_realized_pnl_day", table_name="daily_realized_pnl_snapshots")
    op.drop_index("ix_daily_realized_pnl_user_id", table_name="daily_realized_pnl_snapshots")
    op.drop_table("daily_realized_pnl_snapshots")
