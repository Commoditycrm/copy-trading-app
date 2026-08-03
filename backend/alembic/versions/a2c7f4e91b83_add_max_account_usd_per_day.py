"""add max_account_usd_per_day to subscriber_settings

The daily trading cap ("Max % of account per day") gains a DOLLAR unit
alongside the existing percentage. The two are mutually exclusive: the
Settings UI toggles the unit, "%" persists ``max_account_pct_per_day``
and "$" persists this new ``max_account_usd_per_day`` (each save NULLs
the sibling column). pnl_poller trips the same daily auto-pause when
today's cumulative filled trade notional crosses whichever one is set.

Numeric(20,2) so it can hold account-scale dollar figures (the pct
column is only Numeric(5,2)). Nullable — NULL means this unit isn't in
use.

Revision ID: a2c7f4e91b83
Revises: d5c6b7a8e9f0
Create Date: 2026-08-03 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a2c7f4e91b83'
down_revision: Union[str, None] = 'd5c6b7a8e9f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'subscriber_settings',
        sa.Column('max_account_usd_per_day', sa.Numeric(20, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('subscriber_settings', 'max_account_usd_per_day')
