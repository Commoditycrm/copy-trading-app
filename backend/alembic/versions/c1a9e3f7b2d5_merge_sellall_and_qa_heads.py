"""merge sell-all + qa (discord alert) heads

Joins the sell-all chain (d5c2a9f1b3e7) with the qa/discord chain
(a3e7c19f4b28) so there is a single alembic head. No-op merge.

Revision ID: c1a9e3f7b2d5
Revises: d5c2a9f1b3e7, a3e7c19f4b28
Create Date: 2026-09-03 13:00:00.000000
"""
from typing import Sequence, Union

revision: str = "c1a9e3f7b2d5"
down_revision: Union[str, Sequence[str], None] = ("d5c2a9f1b3e7", "a3e7c19f4b28")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
