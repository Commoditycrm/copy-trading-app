"""dedupe duplicate order rows (same broker_order_id recorded many times)

Backfill/listener re-recording created duplicate `orders` rows sharing one
broker_order_id (one order was recorded 124x). Because realized P&L is computed
from the order rows' own filled_avg_price/filled_quantity (fills table is empty
for these), duplicate FILLED rows DOUBLE-COUNT a trader's realized P&L on the
Calendar. This collapses each (user_id, broker_order_id) group to a single
canonical row, repointing every FK reference first so no bracket/mirror linkage
is orphaned (parent_order_id/bracket_parent_id are ON DELETE SET NULL — a naive
delete would silently null 140 children).

Canonical row per group: prefer a FILLED row (it carries the realized data),
then the earliest created_at. Idempotent — a second run finds no duplicates.

No unique constraint is added here on purpose: the insert path still needs an
ON CONFLICT upsert before a constraint can be enforced without breaking order
recording. That's a separate follow-up; until then this can be re-run to clean
up any dups that reappear.

Revision ID: f7a2d3c1b9e4
Revises: e4c9d2a6b183
"""
from alembic import op

revision = "f7a2d3c1b9e4"
down_revision = "e4c9d2a6b183"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Map each duplicate row → the canonical row to keep for its group.
    op.execute(
        """
        CREATE TEMP TABLE _order_dups AS
        WITH ranked AS (
            SELECT id,
                   first_value(id) OVER (
                       PARTITION BY user_id, broker_order_id
                       ORDER BY (status::text = 'FILLED') DESC, created_at ASC, id ASC
                   ) AS canonical_id
            FROM orders
            WHERE broker_order_id IS NOT NULL
        )
        SELECT id AS dup_id, canonical_id
        FROM ranked
        WHERE id <> canonical_id;
        """
    )

    # Repoint every reference off the duplicate rows onto the canonical row,
    # BEFORE deleting — otherwise ON DELETE SET NULL would drop the linkage.
    op.execute("UPDATE orders o SET parent_order_id = d.canonical_id "
               "FROM _order_dups d WHERE o.parent_order_id = d.dup_id;")
    op.execute("UPDATE orders o SET bracket_parent_id = d.canonical_id "
               "FROM _order_dups d WHERE o.bracket_parent_id = d.dup_id;")
    op.execute("UPDATE fills f SET order_id = d.canonical_id "
               "FROM _order_dups d WHERE f.order_id = d.dup_id;")
    op.execute("UPDATE discord_messages m SET order_id = d.canonical_id "
               "FROM _order_dups d WHERE m.order_id = d.dup_id;")
    op.execute("UPDATE discord_position_guards g SET stop_order_id = d.canonical_id "
               "FROM _order_dups d WHERE g.stop_order_id = d.dup_id;")
    op.execute("UPDATE discord_position_guards g SET entry_order_id = d.canonical_id "
               "FROM _order_dups d WHERE g.entry_order_id = d.dup_id;")

    # Drop the duplicates.
    op.execute("DELETE FROM orders o USING _order_dups d WHERE o.id = d.dup_id;")
    op.execute("DROP TABLE _order_dups;")


def downgrade() -> None:
    # Irreversible data cleanup — deleted duplicate rows are not restorable.
    pass
