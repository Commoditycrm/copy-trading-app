"""restore admin enum label case (user_role 'admin' -> 'ADMIN')

DEF-ADMIN-001. The original fix migration (c4f1a9d3e7b2_fix_admin_enum_label_case)
was lost from the repo — only a stale .pyc remained — so it never re-applies on a
fresh database. The chain that survives is:

    f1a2b3c4d5e6_add_admin_role  →  ALTER TYPE user_role ADD VALUE 'admin'   (lowercase)

but SQLAlchemy's Enum column stores the Python enum *name* — UPPERCASE 'ADMIN'.
On a clean deploy the label stays lowercase 'admin' while the app writes/reads
'ADMIN', so every /api/admin/* 500s (invalid input value for enum user_role).

This migration realigns the label idempotently: it only renames when a lowercase
'admin' exists and 'ADMIN' does not, so it's a no-op on the existing prod/QA
databases (already 'ADMIN' from when the old migration ran) and the actual fix on
a fresh one. Existing rows using the value follow the rename automatically —
RENAME VALUE is a metadata change.

Revision ID: a3f9d1c7e2b8
Revises: a2c7f4e91b83
Create Date: 2026-08-04 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a3f9d1c7e2b8"
down_revision: Union[str, None] = "a2c7f4e91b83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                WHERE t.typname = 'user_role' AND e.enumlabel = 'admin'
            ) AND NOT EXISTS (
                SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                WHERE t.typname = 'user_role' AND e.enumlabel = 'ADMIN'
            ) THEN
                ALTER TYPE user_role RENAME VALUE 'admin' TO 'ADMIN';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                WHERE t.typname = 'user_role' AND e.enumlabel = 'ADMIN'
            ) AND NOT EXISTS (
                SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
                WHERE t.typname = 'user_role' AND e.enumlabel = 'admin'
            ) THEN
                ALTER TYPE user_role RENAME VALUE 'ADMIN' TO 'admin';
            END IF;
        END
        $$;
        """
    )
