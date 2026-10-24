"""Advanced Settings mode for a subscriber

Revision ID: f3a7c1e85b02
Revises: e1b3d7c409fa

One switch: Risk Controls (what every subscriber has today) or Advanced
Settings (per-symbol rules). Only one is active at a time, and the flag is what
decides — symbol rules exist in the database either way, so switching modes
never deletes a configuration, it only stops applying it.

OFF for every existing subscriber, so nothing changes until someone turns it on.

No table for the ALL-Symbols layer: it is a row in subscriber_symbol_settings
under the reserved symbol "ALL". A real ticker is never "ALL", the uniqueness
constraint already makes it one-per-subscriber, and the resolver reads it as
just another overlay — one less table, one less code path.
"""
from alembic import op
import sqlalchemy as sa

revision = "f3a7c1e85b02"
down_revision = "e1b3d7c409fa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriber_settings",
        sa.Column("advanced_settings_enabled", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("subscriber_settings", "advanced_settings_enabled")
