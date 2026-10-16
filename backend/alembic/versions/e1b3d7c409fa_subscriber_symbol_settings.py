"""Per-symbol copy settings for a subscriber

Revision ID: e1b3d7c409fa
Revises: c9f2a71b4e38

A copy subscriber has one set of rules for everything they mirror. This lets
them customise a single symbol — its size, and its way out — without touching
any other symbol and without a Discord channel.

Inert until a row exists: with no row the resolver returns the subscriber's
existing settings and every trade path behaves exactly as it does today, which
is what keeps the trader copy working unchanged.

``settings`` is sparse JSONB (a field is present only when overridden), the
same shape as discord_alert_sources.channel_settings, so both scopes read
through one overlay.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e1b3d7c409fa"
# Re-pointed from c9f2a71b4e38 onto qa's tip when this branch was rebased.
# Both chains forked at c9f2a71b4e38 — qa added the two trader_settings
# Discord columns, this branch added the subscriber ones — which left Alembic
# with two heads and made `upgrade head` ambiguous. The two sets touch
# different tables, so ordering this chain after qa's is safe.
down_revision = "e5a7c9b1d3f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriber_symbol_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        # The underlying root ("AAPL"), never an OCC contract: the exit ladder
        # already keys on the root, so one rule covers the stock and every
        # option on it.
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("settings", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "symbol", name="uq_subscriber_symbol"),
    )
    op.create_index("ix_subscriber_symbol_settings_user_id",
                    "subscriber_symbol_settings", ["user_id"])
    op.create_index("ix_subscriber_symbol_settings_symbol",
                    "subscriber_symbol_settings", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_subscriber_symbol_settings_symbol", table_name="subscriber_symbol_settings")
    op.drop_index("ix_subscriber_symbol_settings_user_id", table_name="subscriber_symbol_settings")
    op.drop_table("subscriber_symbol_settings")
