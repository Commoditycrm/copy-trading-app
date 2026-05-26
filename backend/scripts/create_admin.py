"""Promote an existing user to ADMIN, or create a brand-new admin user.

Run this once on staging/production after the f1a2b3c4d5e6 migration
(add_admin_role) has been applied.

Usage
-----
    cd backend

    # Promote an existing user (most common case):
    python scripts/create_admin.py --email anitha@agilenautics.com

    # Create a brand-new admin user (sets a temporary password):
    python scripts/create_admin.py --email anitha@agilenautics.com --create

IMPORTANT
---------
- Admin accounts are NOT created through the normal /register flow.
- If --create is used, the temporary password is printed once.
  Log in immediately and change it from the settings page.
- Never store the temporary password in source control.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, _BACKEND)

from passlib.hash import bcrypt  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s :: %(message)s")
log = logging.getLogger("create_admin")

_TEMP_PASSWORD = "SignalBoxx-Admin-ChangeMe!"


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote or create an admin user.")
    parser.add_argument("--email", required=True, help="User's email address.")
    parser.add_argument(
        "--create",
        action="store_true",
        help="Create the user if they don't exist (sets a temporary password).",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        user = db.execute(
            select(User).where(User.email == args.email)
        ).scalar_one_or_none()

        if user is not None:
            if user.role == UserRole.ADMIN:
                log.info("%s is already an ADMIN — nothing to do.", args.email)
                return 0
            old_role = user.role.value
            user.role = UserRole.ADMIN
            user.is_active = True
            db.commit()
            log.info(
                "SUCCESS: promoted %s from %s → admin",
                args.email, old_role,
            )
            return 0

        # User does not exist
        if not args.create:
            log.error(
                "No user found with email %s. "
                "Use --create to create a new admin account.",
                args.email,
            )
            return 1

        # Create a new admin user with a temporary password
        pw_hash = bcrypt.hash(_TEMP_PASSWORD)
        user = User(
            id=uuid.uuid4(),
            email=args.email,
            password_hash=pw_hash,
            role=UserRole.ADMIN,
            display_name=args.email.split("@")[0].title(),
            is_active=True,
        )
        db.add(user)
        db.commit()
        log.info("SUCCESS: created new admin user %s", args.email)
        print()
        print("=" * 60)
        print(f"  Admin account created:  {args.email}")
        print(f"  Temporary password:     {_TEMP_PASSWORD}")
        print()
        print("  ⚠️  Log in and change this password IMMEDIATELY.")
        print("=" * 60)
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
