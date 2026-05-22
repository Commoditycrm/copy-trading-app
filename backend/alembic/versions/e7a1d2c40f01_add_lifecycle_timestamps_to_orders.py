"""add 6 lifecycle timestamps to orders

Captures every step of the copy-trade pipeline so the Performance page can
surface per-step latency:

  Parent order (trader-side):
    - trader_submitted_at   : when the trader's submit hits our backend
                              (POST /api/trades) OR when Alpaca first received
                              an externally-placed order
    - socket_received_at    : when our Alpaca trade_updates WebSocket listener
                              receives the event (only meaningful for orders
                              placed outside our app)
    - redis_published_at    : when we publish the SSE 'order.placed' event to
                              Redis pub/sub (the broadcast moment)

  Child order (subscriber-side):
    - subscriber_picked_at  : when the copy_engine begins processing this
                              subscriber for fan-out
    - subscriber_accepted_at: when the subscriber passes eligibility checks
                              and our backend is about to call their broker
    - broker_accepted_at    : when the subscriber's broker (Alpaca) accepted
                              the child order

All nullable: older rows + parent-only / child-only fields are NULL where N/A.

Revision ID: e7a1d2c40f01
Revises: d5b8e3a91f47
Create Date: 2026-05-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7a1d2c40f01"
down_revision: Union[str, None] = "d5b8e3a91f47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_COLUMNS = (
    "trader_submitted_at",
    "socket_received_at",
    "redis_published_at",
    "subscriber_picked_at",
    "subscriber_accepted_at",
    "broker_accepted_at",
)


def upgrade() -> None:
    for name in _NEW_COLUMNS:
        op.add_column(
            "orders",
            sa.Column(name, sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    for name in _NEW_COLUMNS:
        op.drop_column("orders", name)
