"""prevent duplicate FILLED orders — partial unique index on (user_id, broker_order_id)

Root-cause backstop for the order duplication. Order recording is check-then-
insert with no DB guard, so concurrent paths (two fills-sync calls, listener vs
sync) both see "not found" and both INSERT — duplicate FILLED rows that
double-count realized P&L. The app-side fix serializes fills-sync with an
advisory lock and makes synthesis conflict-safe; this index is the hard DB
guarantee behind it.

Scope: a broker order can be FILLED only ONCE, so the index is PARTIAL on
status = 'FILLED'. A blanket unique on (user_id, broker_order_id) would break a
legitimate broker-side MODIFY, which is stored as CANCELED-old + live-replacement
sharing one broker_order_id (snaptrade_listener). Only one of those is FILLED,
so the partial index allows it while blocking the race duplicates.

Runs after the dedupe migration (f7a2d3c1b9e4), so at most one FILLED row per
group already exists; a defensive FILLED-only dedupe guards the index build.

Revision ID: 9c4d7e2f6a13
Revises: f7a2d3c1b9e4
"""
from alembic import op

revision = "9c4d7e2f6a13"
down_revision = "f7a2d3c1b9e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Defensive: collapse any remaining FILLED duplicates before the unique
    # index build (repoint FK refs first so nothing is orphaned). Idempotent.
    op.execute(
        """
        CREATE TEMP TABLE _filled_dups AS
        WITH ranked AS (
            SELECT id,
                   first_value(id) OVER (
                       PARTITION BY user_id, broker_order_id
                       ORDER BY created_at ASC, id ASC
                   ) AS canonical_id
            FROM orders
            WHERE broker_order_id IS NOT NULL AND status::text = 'FILLED'
        )
        SELECT id AS dup_id, canonical_id FROM ranked WHERE id <> canonical_id;
        """
    )
    op.execute("UPDATE orders o SET parent_order_id = d.canonical_id "
               "FROM _filled_dups d WHERE o.parent_order_id = d.dup_id;")
    op.execute("UPDATE orders o SET bracket_parent_id = d.canonical_id "
               "FROM _filled_dups d WHERE o.bracket_parent_id = d.dup_id;")
    op.execute("UPDATE fills f SET order_id = d.canonical_id "
               "FROM _filled_dups d WHERE f.order_id = d.dup_id;")
    op.execute("UPDATE discord_messages m SET order_id = d.canonical_id "
               "FROM _filled_dups d WHERE m.order_id = d.dup_id;")
    op.execute("UPDATE discord_position_guards g SET stop_order_id = d.canonical_id "
               "FROM _filled_dups d WHERE g.stop_order_id = d.dup_id;")
    op.execute("UPDATE discord_position_guards g SET entry_order_id = d.canonical_id "
               "FROM _filled_dups d WHERE g.entry_order_id = d.dup_id;")
    op.execute("DELETE FROM orders o USING _filled_dups d WHERE o.id = d.dup_id;")
    op.execute("DROP TABLE _filled_dups;")

    op.execute(
        """
        CREATE UNIQUE INDEX uq_orders_user_filled_broker_order
        ON orders (user_id, broker_order_id)
        WHERE broker_order_id IS NOT NULL AND status = 'FILLED';
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_orders_user_filled_broker_order;")
