"""unify the trim-ladder and trail-down heads

The history branched: orders.trail_down_percent (c9d0e1f2a3b4) and the Discord
trim ladder (b2e7f4a09c15) both descend from b8d1f4a2c6e9 without either
knowing about the other. Two heads make `alembic upgrade head` ambiguous, so it
refuses to run — which is why CI rejects it rather than letting a deploy
discover the problem.

Both branches are real and already applied in different places, so neither can
be dropped. This merge only rejoins them; it changes no schema, which is why
upgrade and downgrade are empty.

Revision ID: a6f9c2d5f8bc
Revises: b2e7f4a09c15, c9d0e1f2a3b4
Create Date: 2026-09-17
"""
from typing import Sequence, Union

revision: str = "a6f9c2d5f8bc"
down_revision: Union[str, Sequence[str], None] = ("b2e7f4a09c15", "c9d0e1f2a3b4")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Nothing to do — a merge point carries no schema change."""


def downgrade() -> None:
    """Nothing to undo."""
