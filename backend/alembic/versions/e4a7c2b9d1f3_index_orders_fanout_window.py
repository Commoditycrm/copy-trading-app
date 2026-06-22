"""index orders for the dashboard fan-out window queries

The admin Performance dashboard filters parent fan-outs by
``COALESCE(trader_submitted_at, created_at)`` over a (trader, time) window. A
plain column index can't serve a ``COALESCE()`` predicate, so add a *partial
expression* index on the coalesced timestamp, restricted to the exact set the
window query scans (parent orders that were fanned out). This turns the
otherwise-sequential scan into an index range scan.

``parent_order_id`` is already indexed (covers the child IN-list lookup), so no
change is needed there.

Note: this is a plain (non-CONCURRENT) CREATE INDEX — fine for QA / early-prod
volumes. If the ``orders`` table grows large, recreate it with
``postgresql_concurrently=True`` (outside a transaction) to avoid a write lock.

Revision ID: e4a7c2b9d1f3
Revises: d3f8b1c0a2e4
Create Date: 2026-06-22 13:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4a7c2b9d1f3"
down_revision: Union[str, None] = "d3f8b1c0a2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_orders_fanout_window",
        "orders",
        [sa.text("COALESCE(trader_submitted_at, created_at)")],
        postgresql_where=sa.text("parent_order_id IS NULL AND fanned_out_to_subscribers IS TRUE"),
    )


def downgrade() -> None:
    op.drop_index("ix_orders_fanout_window", table_name="orders")
