"""fix admin user_role label case (admin -> ADMIN)

BUG-AUTH-001: the model maps Enum(UserRole) by member NAME, so it persists
TRADER / SUBSCRIBER / ADMIN (uppercase). The earlier add-admin migration
(f1a2b3c4d5e6) added the label lowercase as 'admin', so the ORM emitted 'ADMIN'
(invalid → DataError on insert) and couldn't read a stored 'admin' row
(LookupError). Rename the value to match the rest of the enum. RENAME VALUE
updates every row using it in place, so any operator-seeded admin survives.

Revision ID: c4f1a9d3e7b2
Revises: f1e2d3c4b5a6
Create Date: 2026-06-29 20:30:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c4f1a9d3e7b2"
down_revision: Union[str, None] = "f1e2d3c4b5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # RENAME VALUE (unlike ADD VALUE) is transaction-safe, so no autocommit_block.
    op.execute("ALTER TYPE user_role RENAME VALUE 'admin' TO 'ADMIN'")


def downgrade() -> None:
    op.execute("ALTER TYPE user_role RENAME VALUE 'ADMIN' TO 'admin'")
