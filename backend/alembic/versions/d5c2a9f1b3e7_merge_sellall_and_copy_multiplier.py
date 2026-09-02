"""merge sell-all snapshot + copy_multiplier heads

Both branch from b1d7f3e9a2c4; this is a no-op merge so there is a single head.

Revision ID: d5c2a9f1b3e7
Revises: a7c1e9f2b4d8, f2b8c1d4e7a9
Create Date: 2026-09-02 12:00:00.000000
"""
from typing import Sequence, Union

revision: str = "d5c2a9f1b3e7"
down_revision: Union[str, Sequence[str], None] = ("a7c1e9f2b4d8", "f2b8c1d4e7a9")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
