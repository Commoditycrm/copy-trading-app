"""merge discord heads

Revision ID: 3551c0d35725
Revises: c9d0e1f2a3b4, b2e7f4a09c15
Create Date: 2026-09-17 15:22:40.044939

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3551c0d35725'
down_revision: Union[str, None] = ('c9d0e1f2a3b4', 'b2e7f4a09c15')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
