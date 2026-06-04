"""add auto_liquidation_limit + auto_liquidated_at to subscriber_settings

`auto_liquidation_limit` is the dollar floor on the subscriber's account
equity. When the pnl_poller observes equity <= this limit, copy is
auto-disabled AND every open position on the subscriber's broker is
liquidated at market. `auto_liquidated_at` stamps the trigger time so
the Settings page can show "auto-liquidated at HH:MM" and so re-trigger
audits have something concrete to point at.

Both columns are nullable so existing subscribers default to the
feature being OFF (NULL = disabled).

Revision ID: 9a4c2e7b8d5f
Revises: 8f4e2c1d6b9a
Create Date: 2026-06-04 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9a4c2e7b8d5f'
down_revision: Union[str, None] = '8f4e2c1d6b9a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'subscriber_settings',
        sa.Column('auto_liquidation_limit', sa.Numeric(20, 2), nullable=True),
    )
    op.add_column(
        'subscriber_settings',
        sa.Column('auto_liquidated_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('subscriber_settings', 'auto_liquidated_at')
    op.drop_column('subscriber_settings', 'auto_liquidation_limit')
