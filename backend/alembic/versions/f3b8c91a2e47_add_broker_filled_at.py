"""add orders.broker_filled_at (broker's own execution timestamp)

Order History's "Time Taken to Filled" column computed
``submitted_at -> closed_at``. ``closed_at`` is written by every terminal-status
path as ``datetime.now(timezone.utc)`` — the moment WE OBSERVED the fill, not the
moment it traded. So on SnapTrade and Webull the column was reporting our own
detection lag under a label that reads as broker latency.

Alpaca was already right, but only incidentally: it has ``fills`` rows whose
``filled_at`` is the activities feed's ``transaction_time``, and the frontend
prefers those. ``fills_sync.sync_account_fills`` early-returns unless the adapter
is Alpaca, so no other broker has fill rows to prefer.

This column stores the broker's own timestamp where the broker reports one
(SnapTrade exposes ``time_executed`` on its order record; Webull reports a filled
time on the order-detail / day-orders payloads). Keeping it SEPARATE from
closed_at rather than overwriting closed_at is deliberate: the difference
between the two IS the detection lag, which is the number that tells us whether
a fill-sync change actually worked.

Nullable with no backfill. The broker's execution time for an order that already
terminalized is not recoverable from our side, and guessing it would put made-up
timestamps in the column meant to be the trustworthy one. Historical rows stay
NULL and readers fall back to closed_at, flagged as approximate.

Revision ID: f3b8c91a2e47
Revises: e7c1d2a3b4f5
"""
from alembic import op
import sqlalchemy as sa

revision = "f3b8c91a2e47"
down_revision = "e7c1d2a3b4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("broker_filled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "broker_filled_at")
