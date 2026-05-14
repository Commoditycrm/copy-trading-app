"""drop subscription_tier from subscriber_settings

Revision ID: b4ff20d653a3
Revises: 652699d1385e
Create Date: 2026-05-12 14:39:53.749529

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4ff20d653a3'
down_revision: Union[str, None] = '652699d1385e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('subscriber_settings', 'subscription_tier')


def downgrade() -> None:
    op.add_column(
        'subscriber_settings',
        sa.Column(
            'subscription_tier',
            sa.VARCHAR(length=40),
            nullable=False,
            server_default='basic',
        ),
    )
