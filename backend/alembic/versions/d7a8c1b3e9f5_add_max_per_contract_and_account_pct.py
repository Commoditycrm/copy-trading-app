"""add max_per_contract and max_account_pct_per_day to subscriber_settings

Two new optional knobs on subscriber_settings:
  - max_per_contract: UI-only field (no enforcement) for showing a
    per-contract dollar ceiling. Persisted so it survives refresh.
  - max_account_pct_per_day: % of current account equity that, if today's
    P&L hits as a loss, auto-pauses copy. Enforced by pnl_poller every 60s
    using ``equity * pct / 100`` as the dollar threshold.

Revision ID: d7a8c1b3e9f5
Revises: faca94c189a1
Create Date: 2026-06-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7a8c1b3e9f5"
down_revision: Union[str, None] = "faca94c189a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscriber_settings",
        sa.Column("max_per_contract", sa.Numeric(precision=20, scale=2), nullable=True),
    )
    # Stored as a percent value (e.g. 50.00 = 50%). Bounded 0–100 at the
    # API layer, not the DB, so old rows with NULL stay valid.
    op.add_column(
        "subscriber_settings",
        sa.Column("max_account_pct_per_day", sa.Numeric(precision=5, scale=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriber_settings", "max_account_pct_per_day")
    op.drop_column("subscriber_settings", "max_per_contract")
