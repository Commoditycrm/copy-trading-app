"""add daily_profit_target_pct + profit_target_hit_at to subscriber_settings

Adds the configurable daily PROFIT TARGET (percent of the previous day's account
value). Distinct from daily_profit_limit_pct: the target LIQUIDATES open
positions once to book the day's gain and then leaves copy ON, versus the limit
which pauses copy. profit_target_hit_at is the once-per-day guard.

Idempotent (ADD COLUMN IF NOT EXISTS) so re-running against a DB that already
has the columns is a no-op.

Revision ID: c9f1a2b3d4e5
Revises: b7d4e2f1a9c3
Create Date: 2026-07-23 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c9f1a2b3d4e5"
down_revision: Union[str, None] = "b7d4e2f1a9c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE subscriber_settings "
        "ADD COLUMN IF NOT EXISTS daily_profit_target_pct NUMERIC(5, 2)"
    )
    op.execute(
        "ALTER TABLE subscriber_settings "
        "ADD COLUMN IF NOT EXISTS profit_target_hit_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE subscriber_settings DROP COLUMN IF EXISTS profit_target_hit_at")
    op.execute("ALTER TABLE subscriber_settings DROP COLUMN IF EXISTS daily_profit_target_pct")
