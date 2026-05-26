"""add 'snaptrade' value to broker_name enum

Adds SnapTrade as a third supported broker. SnapTrade is an aggregator —
unlike Alpaca (direct WebSocket) and Webull (unofficial polling), the
user is redirected to SnapTrade's connection portal and we never see
their broker credentials. We get back a per-connection
``authorization_id`` + ``account_id`` we can use to read orders /
positions and submit trades.

See backend/app/brokers/snaptrade.py for the adapter and
backend/app/services/snaptrade_listener.py for the polling loop.

Revision ID: f3c9d7b1e042
Revises: e8b2f7c3a91d
Create Date: 2026-05-26 13:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f3c9d7b1e042"
down_revision: Union[str, None] = "e8b2f7c3a91d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE broker_name ADD VALUE IF NOT EXISTS 'snaptrade'")


def downgrade() -> None:
    # Same reasoning as c8d3f5a92e14 / e8b2f7c3a91d — Postgres can't drop
    # enum values without recreating the type. Accept the residual value.
    pass
