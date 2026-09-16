"""add orders.is_partial_close (a close that does not flatten)

A Discord trim sells part of a position and keeps the rest. It is a close —
is_closing stays True so the sell is marked SELL_TO_CLOSE and the subscriber's
close-side quantity clamp applies — but the trader has NOT left the name.

The copy engine needs that distinction. It reads a trader close as "their
accumulation window is over" and cancels the subscriber's still-working entry on
the same contract. Correct on a real exit; on a trim it would strand the
subscriber out of a trade the trader is still in.

Off by default, so every existing order and every non-trim close behaves exactly
as before.

Revision ID: a1d5c8e30f76
Revises: d4e9c1a7b60f
Create Date: 2026-09-16
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1d5c8e30f76"
down_revision: Union[str, None] = "d4e9c1a7b60f"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "is_partial_close", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("orders", "is_partial_close")
