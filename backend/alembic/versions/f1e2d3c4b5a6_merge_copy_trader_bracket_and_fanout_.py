"""merge copy_trader_bracket and fanout_index heads

Revision ID: f1e2d3c4b5a6
Revises: d4e5f6a7b8c9, e4a7c2b9d1f3
Create Date: 2026-06-25 15:50:18.665128

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1e2d3c4b5a6'
down_revision: Union[str, None] = ('d4e5f6a7b8c9', 'e4a7c2b9d1f3')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
