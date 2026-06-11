"""add daily_equity_snapshots

Broker-agnostic day-start equity table. ``pnl_poller`` snapshots the
first equity observation of each UTC day per broker account, then
uses that as the baseline to compute ``todays_pl = equity - day_start``
when the broker itself doesn't expose a day-start figure (e.g.
SnapTrade-routed Alpaca paper accounts).

See app/models/daily_equity_snapshot.py for the full rationale.

Revision ID: e9f1c7d2a803
Revises: d8b4c5e7f190
Create Date: 2026-06-10 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e9f1c7d2a803"
down_revision: Union[str, None] = "d8b4c5e7f190"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_equity_snapshots",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "broker_account_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("broker_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("utc_date", sa.Date(), nullable=False),
        sa.Column("equity", sa.Numeric(20, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "broker_account_id", "utc_date",
            name="uq_daily_equity_account_date",
        ),
    )
    op.create_index(
        "ix_daily_equity_snapshots_broker_account_id",
        "daily_equity_snapshots",
        ["broker_account_id"],
    )
    op.create_index(
        "ix_daily_equity_snapshots_utc_date",
        "daily_equity_snapshots",
        ["utc_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_daily_equity_snapshots_utc_date", table_name="daily_equity_snapshots")
    op.drop_index("ix_daily_equity_snapshots_broker_account_id", table_name="daily_equity_snapshots")
    op.drop_table("daily_equity_snapshots")
