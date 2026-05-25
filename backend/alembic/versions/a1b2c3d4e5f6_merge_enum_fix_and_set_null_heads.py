"""merge enum-case-drift and orders-broker-account-id-set-null heads

Two migrations were authored against the same parent revision
``c8d3f5a92e14`` in parallel PRs that landed close together:

    c8d3f5a92e14 ──┬── d9f4a82e617c  (orders.broker_account_id ON DELETE SET NULL)
                   └── f9c2e3d8a1b7  (fix enum case drift)

That left two heads, which makes ``alembic upgrade head`` ambiguous and
breaks the deploy. This empty migration merges them into a single head
so the chain is linear again. Pure no-op: no schema changes.

Revision ID: a1b2c3d4e5f6
Revises: d9f4a82e617c, f9c2e3d8a1b7
Create Date: 2026-05-25 19:30:00.000000
"""
from typing import Sequence, Union


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = ("d9f4a82e617c", "f9c2e3d8a1b7")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
