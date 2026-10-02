"""Resting take-profit orders for the exit ladder

trader_settings.discord_tp_orders — the fourth exit choice: each trim rests at
the broker as a limit order (paired with its stop where the broker links them).
discord_position_guards.tp_* — the take-profit currently resting for a holding.

Revision ID: a7b9c1d3e5f4
Revises: f6a8b0c2d4e3
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a7b9c1d3e5f4"
down_revision = "f6a8b0c2d4e3"
branch_labels = None
depends_on = None

_G = "discord_position_guards"


def upgrade() -> None:
    op.add_column("trader_settings", sa.Column(
        "discord_tp_orders", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    for name in ("tp_order_id", "tp_stop_order_id"):
        op.add_column(_G, sa.Column(
            name, postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True))
    op.add_column(_G, sa.Column("tp_rung", sa.Integer(), nullable=True))
    op.add_column(_G, sa.Column("tp_qty", sa.Numeric(18, 6), nullable=True))
    op.add_column(_G, sa.Column(
        "tp_off", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column(_G, sa.Column("tp_backoff_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for name in ("tp_backoff_until", "tp_off", "tp_qty", "tp_rung", "tp_stop_order_id", "tp_order_id"):
        op.drop_column(_G, name)
    op.drop_column("trader_settings", "discord_tp_orders")
