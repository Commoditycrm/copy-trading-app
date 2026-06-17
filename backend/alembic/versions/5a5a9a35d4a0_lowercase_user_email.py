"""lowercase_user_email

One-time normalisation of ``users.email`` to lowercase so the now-
case-insensitive login flow (frontend lowercases input, RegisterIn /
LoginIn lowercase in pydantic) matches what's stored in the DB.

The lookup in app/api/auth.py is plain equality (``User.email ==
payload.email``). Without this migration, anyone who registered with
uppercase characters before today would be unable to sign in.

Safety: ``users.email`` carries a unique constraint, so two pre-existing
rows that only differ in case (``alice@x.com`` and ``Alice@x.com``)
would collide after LOWER(). The upgrade detects those collisions
upfront and aborts with a clear error listing the offenders so a human
can decide which row wins — better than half-applying the UPDATE and
leaving the table in an inconsistent state.

Revision ID: 5a5a9a35d4a0
Revises: e9f1c7d2a803
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op


revision: str = "5a5a9a35d4a0"
down_revision: Union[str, None] = "e9f1c7d2a803"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # Step 1 — detect collisions BEFORE updating.
    # Group all emails by their lowercase form; flag any group with > 1
    # row. These rows can't be auto-resolved (which user's history wins?)
    # so we surface them and stop.
    collisions = bind.exec_driver_sql(
        """
        SELECT LOWER(email) AS norm,
               COUNT(*)     AS n,
               ARRAY_AGG(email ORDER BY email) AS variants
        FROM users
        GROUP BY LOWER(email)
        HAVING COUNT(*) > 1
        ORDER BY norm
        """
    ).fetchall()

    if collisions:
        lines = [
            f"  {row.norm!r}: {row.n} rows -> {row.variants}"
            for row in collisions
        ]
        msg = (
            "Cannot lowercase users.email — pre-existing rows would "
            "collide on the unique index. Resolve manually and re-run:\n"
            + "\n".join(lines)
        )
        raise RuntimeError(msg)

    # Step 2 — UPDATE in-place. Only touch rows that actually differ
    # so we don't churn the index for already-lowercase data.
    bind.exec_driver_sql(
        "UPDATE users SET email = LOWER(email) WHERE email <> LOWER(email)"
    )


def downgrade() -> None:
    # No-op: case information is irretrievable once dropped. The
    # case-insensitive auth flow stays consistent on rollback because
    # the schema-level lowercase normalizers in app/schemas/auth.py
    # would also need to be reverted for any user to re-introduce mixed
    # case — and at that point they'd just re-register with whatever
    # case they want.
    pass
