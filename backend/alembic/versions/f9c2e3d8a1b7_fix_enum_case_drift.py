"""fix enum case drift: align order_status and retry_interval with SQLAlchemy

The previous migration b3c1d4e2a51f added Postgres enum values in
lowercase ('retry_pending', 'never', '1m', '2m', '3m', '5m'). But
SQLAlchemy's default Enum() column stores Python enum *names* (uppercase) —
so the application writes 'RETRY_PENDING', 'NEVER', 'ONE_M', etc. The
result: runtime errors on any query touching these columns:

    psycopg.InvalidTextRepresentation: invalid input value for enum
    order_status: "RETRY_PENDING"

    LookupError: 'never' is not among the defined enum values.
    Enum name: retry_interval. Possible values: NEVER, ONE_M, ...

This migration restores alignment by:
  1. Adding the missing uppercase value to order_status (RETRY_PENDING)
  2. Adding all uppercase values to retry_interval
     (NEVER, ONE_M, TWO_M, THREE_M, FOUR_M, FIVE_M)
  3. Migrating any pre-existing lowercase rows in orders.status and
     subscriber_settings.retry_interval_{open,close} to the uppercase form
  4. Updating the server_default on the two retry_interval columns to
     'NEVER' so non-SQLAlchemy inserts pick the right value too

Idempotent — safe to re-run. The lowercase orphan values remain in both
enums (Postgres doesn't cleanly support dropping enum values without
recreating the whole type, which is destructive). They're harmless because
nothing in the application produces them anymore.

Revision ID: f9c2e3d8a1b7
Revises: c8d3f5a92e14
Create Date: 2026-05-25 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f9c2e3d8a1b7"
down_revision: Union[str, None] = "c8d3f5a92e14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Add uppercase enum values ──────────────────────────────────────
    # ALTER TYPE ... ADD VALUE can't run inside a transaction in Postgres
    # before v12. All currently-supported versions are 12+.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE order_status ADD VALUE IF NOT EXISTS 'RETRY_PENDING'"
        )
        op.execute(
            "ALTER TYPE retry_interval ADD VALUE IF NOT EXISTS 'NEVER'"
        )
        op.execute(
            "ALTER TYPE retry_interval ADD VALUE IF NOT EXISTS 'ONE_M'"
        )
        op.execute(
            "ALTER TYPE retry_interval ADD VALUE IF NOT EXISTS 'TWO_M'"
        )
        op.execute(
            "ALTER TYPE retry_interval ADD VALUE IF NOT EXISTS 'THREE_M'"
        )
        op.execute(
            "ALTER TYPE retry_interval ADD VALUE IF NOT EXISTS 'FOUR_M'"
        )
        op.execute(
            "ALTER TYPE retry_interval ADD VALUE IF NOT EXISTS 'FIVE_M'"
        )

    # ── 2. Migrate any pre-existing lowercase data ────────────────────────
    # On prod-with-manual-patches: these UPDATEs match zero rows (no-op).
    # On a fresh DB seeded against the old migration: these convert any
    # orphan rows to the new convention.
    op.execute(
        "UPDATE orders SET status = 'RETRY_PENDING'::order_status "
        "WHERE status::text = 'retry_pending'"
    )
    op.execute(
        """
        UPDATE subscriber_settings
        SET retry_interval_open = CASE retry_interval_open::text
                WHEN 'never' THEN 'NEVER'::retry_interval
                WHEN '1m'    THEN 'ONE_M'::retry_interval
                WHEN '2m'    THEN 'TWO_M'::retry_interval
                WHEN '3m'    THEN 'THREE_M'::retry_interval
                WHEN '5m'    THEN 'FIVE_M'::retry_interval
                ELSE retry_interval_open
            END,
            retry_interval_close = CASE retry_interval_close::text
                WHEN 'never' THEN 'NEVER'::retry_interval
                WHEN '1m'    THEN 'ONE_M'::retry_interval
                WHEN '2m'    THEN 'TWO_M'::retry_interval
                WHEN '3m'    THEN 'THREE_M'::retry_interval
                WHEN '5m'    THEN 'FIVE_M'::retry_interval
                ELSE retry_interval_close
            END
        """
    )

    # ── 3. Realign the server_default ─────────────────────────────────────
    # SQLAlchemy inserts always specify the column, so server_default is
    # only consulted for raw SQL inserts. Still — keep it consistent so a
    # future raw-SQL writer doesn't reintroduce lowercase rows.
    op.alter_column(
        "subscriber_settings",
        "retry_interval_open",
        server_default="NEVER",
    )
    op.alter_column(
        "subscriber_settings",
        "retry_interval_close",
        server_default="NEVER",
    )


def downgrade() -> None:
    # Restore the old server_default
    op.alter_column(
        "subscriber_settings",
        "retry_interval_open",
        server_default="never",
    )
    op.alter_column(
        "subscriber_settings",
        "retry_interval_close",
        server_default="never",
    )

    # Reverse-migrate data
    op.execute(
        "UPDATE orders SET status = 'retry_pending'::order_status "
        "WHERE status::text = 'RETRY_PENDING'"
    )
    op.execute(
        """
        UPDATE subscriber_settings
        SET retry_interval_open = CASE retry_interval_open::text
                WHEN 'NEVER'   THEN 'never'::retry_interval
                WHEN 'ONE_M'   THEN '1m'::retry_interval
                WHEN 'TWO_M'   THEN '2m'::retry_interval
                WHEN 'THREE_M' THEN '3m'::retry_interval
                WHEN 'FIVE_M'  THEN '5m'::retry_interval
                ELSE retry_interval_open
            END,
            retry_interval_close = CASE retry_interval_close::text
                WHEN 'NEVER'   THEN 'never'::retry_interval
                WHEN 'ONE_M'   THEN '1m'::retry_interval
                WHEN 'TWO_M'   THEN '2m'::retry_interval
                WHEN 'THREE_M' THEN '3m'::retry_interval
                WHEN 'FIVE_M'  THEN '5m'::retry_interval
                ELSE retry_interval_close
            END
        """
    )
    # Note: Postgres can't cleanly drop enum values without recreating
    # the type, so the uppercase values remain as orphans after downgrade.
