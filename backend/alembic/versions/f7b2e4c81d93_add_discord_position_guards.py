"""track SELL-alert count per Discord position (trail then close)

Revision ID: f7b2e4c81d93
Revises: e4a91c72b508
Create Date: 2026-09-15 00:00:00.000000

An exit alert means different things depending on what came before it — the
first arms a trailing stop, the second closes — so the count has to be stored.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "f7b2e4c81d93"
down_revision: Union[str, None] = "e4a91c72b508"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "discord_position_guards",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(40), nullable=False),
        sa.Column("option_strike", sa.Numeric(18, 4), nullable=True),
        sa.Column("option_right", sa.String(4), nullable=True),
        sa.Column("option_expiry", sa.Date(), nullable=True),
        sa.Column("sell_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trail_percent", sa.Numeric(9, 4), nullable=True),
        sa.Column("peak_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("armed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_order_id", UUID(as_uuid=True),
                  sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_discord_position_guards_user_id", "discord_position_guards", ["user_id"])
    op.create_index("ix_discord_position_guards_symbol", "discord_position_guards", ["symbol"])
    # One LIVE guard per contract: a second would double-count sells and could
    # fire two closes for the same position.
    op.create_unique_constraint(
        "uq_discord_guard_contract", "discord_position_guards",
        ["user_id", "symbol", "option_strike", "option_right", "option_expiry", "closed_at"],
    )
    # The trail used when a position's stop is armed.
    op.add_column(
        "trader_settings",
        sa.Column("discord_trail_percent", sa.Numeric(9, 4), nullable=False,
                  server_default="20"),
    )


def downgrade() -> None:
    op.drop_column("trader_settings", "discord_trail_percent")
    op.drop_constraint("uq_discord_guard_contract", "discord_position_guards", type_="unique")
    op.drop_index("ix_discord_position_guards_symbol", table_name="discord_position_guards")
    op.drop_index("ix_discord_position_guards_user_id", table_name="discord_position_guards")
    op.drop_table("discord_position_guards")
