"""add the Discord entry reprice: order marker + thresholds

A Discord buy alert is placed as a LIMIT at the price the alert named. When the
contract moves before we get there, that limit rests unfilled and the trade is
missed — the alert was right, the fill just never happened.

So an unfilled Discord entry gets ONE repriced attempt. orders.discord_repriced_at
records that it has had it, which is what keeps this to a single retry rather
than a chase: the scanner only picks up rows where it is NULL, and stamping it is
part of the same transaction as the reprice.

The two thresholds live on trader_settings so they can be tuned without a code
change. Defaults are the agreed behaviour: wait 30s, then retry 10% above the
original limit.

Revision ID: c3b8e1d94f27
Revises: a6f9c2d5f8bc
Create Date: 2026-09-17
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3b8e1d94f27"
down_revision: Union[str, None] = "a6f9c2d5f8bc"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("discord_repriced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "trader_settings",
        sa.Column(
            "discord_reprice_after_seconds", sa.Integer(),
            nullable=False, server_default="30",
        ),
    )
    op.add_column(
        "trader_settings",
        sa.Column(
            "discord_reprice_pct", sa.Numeric(9, 4),
            nullable=False, server_default="10",
        ),
    )


def downgrade() -> None:
    op.drop_column("trader_settings", "discord_reprice_pct")
    op.drop_column("trader_settings", "discord_reprice_after_seconds")
    op.drop_column("orders", "discord_repriced_at")
