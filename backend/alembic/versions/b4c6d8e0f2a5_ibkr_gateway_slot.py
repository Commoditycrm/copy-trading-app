"""Hosted IBKR gateway slot on broker_accounts

Revision ID: b4c6d8e0f2a5
Revises: a3b5c7d9e1f4
"""
from alembic import op
import sqlalchemy as sa

revision = "b4c6d8e0f2a5"
down_revision = "a3b5c7d9e1f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("broker_accounts", sa.Column("ibkr_gateway_slot", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_broker_accounts_ibkr_gateway_slot", "broker_accounts", ["ibkr_gateway_slot"])


def downgrade() -> None:
    op.drop_constraint("uq_broker_accounts_ibkr_gateway_slot", "broker_accounts", type_="unique")
    op.drop_column("broker_accounts", "ibkr_gateway_slot")
