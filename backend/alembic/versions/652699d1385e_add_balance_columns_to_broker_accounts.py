"""add balance columns to broker_accounts

Revision ID: 652699d1385e
Revises: 4e2dad6c1805
Create Date: 2026-05-12 14:20:09.716245

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '652699d1385e'
down_revision: Union[str, None] = '4e2dad6c1805'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('broker_accounts', sa.Column('cash', sa.Numeric(precision=20, scale=4), nullable=True))
    op.add_column('broker_accounts', sa.Column('buying_power', sa.Numeric(precision=20, scale=4), nullable=True))
    op.add_column('broker_accounts', sa.Column('total_equity', sa.Numeric(precision=20, scale=4), nullable=True))
    op.add_column('broker_accounts', sa.Column('currency', sa.String(length=8), nullable=True))
    op.add_column('broker_accounts', sa.Column('balance_updated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('broker_accounts', 'balance_updated_at')
    op.drop_column('broker_accounts', 'currency')
    op.drop_column('broker_accounts', 'total_equity')
    op.drop_column('broker_accounts', 'buying_power')
    op.drop_column('broker_accounts', 'cash')
