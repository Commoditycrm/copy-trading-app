"""add bracket_parent_id + bracket_leg link columns to orders

Links bracket TP/SL exit orders back to their entry, for brokers that
don't support native bracket OCO (everything except Alpaca direct).
The emulator service uses these to:
  - place TP/SL exits when the entry fills (lookup: bracket_parent_id IS NULL
    entries with non-null take_profit_price / stop_loss_price);
  - cancel the sibling when one exit fills (lookup: same bracket_parent_id,
    opposite bracket_leg);
  - keep idempotency (don't double-place if exits already exist).

We deliberately keep this separate from ``parent_order_id`` — that one is
already used to link subscriber-mirror children to the trader's entry,
and overloading it for bracket-exit linkage would tangle the copy-engine
queries.

Revision ID: 8f4e2c1d6b9a
Revises: 7d3b9f2e8c4a
Create Date: 2026-06-04 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = '8f4e2c1d6b9a'
down_revision: Union[str, None] = '7d3b9f2e8c4a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'orders',
        sa.Column('bracket_parent_id', UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        'orders',
        sa.Column('bracket_leg', sa.String(length=4), nullable=True),
    )
    op.create_foreign_key(
        'fk_orders_bracket_parent_id',
        'orders', 'orders',
        ['bracket_parent_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        'ix_orders_bracket_parent_id', 'orders', ['bracket_parent_id']
    )


def downgrade() -> None:
    op.drop_index('ix_orders_bracket_parent_id', table_name='orders')
    op.drop_constraint('fk_orders_bracket_parent_id', 'orders', type_='foreignkey')
    op.drop_column('orders', 'bracket_leg')
    op.drop_column('orders', 'bracket_parent_id')
