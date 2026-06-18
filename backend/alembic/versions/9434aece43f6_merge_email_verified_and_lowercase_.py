"""merge email_verified and lowercase_email heads

Revision ID: 9434aece43f6
Revises: 5a5a9a35d4a0, c5e91a7b3d24
Create Date: 2026-06-18 10:35:08.400836

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9434aece43f6'
down_revision: Union[str, None] = ('5a5a9a35d4a0', 'c5e91a7b3d24')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
