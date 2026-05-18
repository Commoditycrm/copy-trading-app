"""add fanned_out_to_subscribers to orders

Revision ID: d5b8e3a91f47
Revises: c2a1f4e7b9d2
Create Date: 2026-05-18 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5b8e3a91f47"
down_revision: Union[str, None] = "c2a1f4e7b9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "fanned_out_to_subscribers",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("orders", "fanned_out_to_subscribers", server_default=None)


def downgrade() -> None:
    op.drop_column("orders", "fanned_out_to_subscribers")
