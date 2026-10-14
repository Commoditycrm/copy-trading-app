"""add orders.detected_via — which path told us about the order

"Is the realtime stream actually doing the work, or is the REST poller still
carrying everything?" could not be answered from the data. Both paths write
through the same code and leave no trace, so a stream that was connected but
discarding every event looked identical to one carrying the whole load. Two
months went by on the wrong side of that question.

"stream" = the broker's realtime push told us; "poll" = our REST sweep did.
NULL for brokers and paths that set neither, and for every row written before
this column existed — no backfill is possible, because the information was
never recorded.

Short String rather than an enum on purpose: adding a value later must not need
a migration, and this is diagnostic metadata, not something the engine branches
on.

Revision ID: c9f2a71b4e38
Revises: b4c6d8e0f2a5
"""
from alembic import op
import sqlalchemy as sa

revision = "c9f2a71b4e38"
down_revision = "b4c6d8e0f2a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("detected_via", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "detected_via")
