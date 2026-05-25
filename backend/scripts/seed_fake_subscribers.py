"""Seed (or clean up) fake subscribers for load-testing the fanout pipeline.

These users + broker accounts route to FakeBrokerAdapter (a no-op sleep)
instead of Alpaca. Use them to measure platform-side latency on the
Performance page without hitting Alpaca's rate limits or burning paper
accounts.

Usage
-----
    cd backend
    python scripts/seed_fake_subscribers.py seed --count 100 --trader-email <trader@example.com>
    python scripts/seed_fake_subscribers.py count
    python scripts/seed_fake_subscribers.py cleanup

Idempotent: re-running `seed` will create only the missing users, not
duplicate the existing ones (lookup is by the deterministic email
pattern). All seeded users have email like
``fake-load-test-{N}@example.invalid`` so they're trivially identifiable
and the cleanup command can find them all without a separate registry.

The seeded users:
  - role = SUBSCRIBER, is_active = True
  - SubscriberSettings: copy_enabled=True, multiplier=1.0, following the
    specified trader.
  - One BrokerAccount per user: broker=FAKE, encrypted_credentials =
    encrypt_json({}) so the decrypt path in copy_engine succeeds without
    branching on broker type.

After seeding, the script invalidates the relevant Redis caches so the
fanout picks them up on the next trade without waiting for TTL.

WARNING — production safety
---------------------------
The seeded users will receive mirror orders the moment the trader places
a real trade. The orders won't reach a real broker (FakeBrokerAdapter
just sleeps), so they're harmless from a money standpoint, but they DO
fan out and consume real DB rows + Redis stream slots. Don't seed against
a production database unless you intend to load-test it.
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid
from decimal import Decimal
from typing import Optional

# Make `app` importable when run from backend/ — same trick as worker.py
# in the anitha-workspace branch.
import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, _BACKEND)

from passlib.hash import bcrypt  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.broker_account import BrokerAccount, BrokerName  # noqa: E402
from app.models.settings import SubscriberSettings  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from app.services.crypto import encrypt_json  # noqa: E402

log = logging.getLogger("seed_fake_subscribers")

# All seeded users have this email pattern so cleanup is trivial.
EMAIL_PREFIX = "fake-load-test-"
EMAIL_DOMAIN = "@example.invalid"
# .invalid is the IETF-reserved TLD for guaranteed-non-resolvable hostnames
# — perfect for test fixtures that should never reach anything real.


def _email_for(index: int) -> str:
    return f"{EMAIL_PREFIX}{index:04d}{EMAIL_DOMAIN}"


def _is_seeded_email(email: str) -> bool:
    return email.startswith(EMAIL_PREFIX) and email.endswith(EMAIL_DOMAIN)


def cmd_seed(count: int, trader_email: str, multiplier: Decimal) -> int:
    """Create up to `count` fake subscribers following the trader. Returns
    the number of *new* users created (existing ones are left as-is)."""
    if count <= 0:
        log.error("--count must be positive")
        return 2

    with SessionLocal() as db:
        trader = db.execute(
            select(User).where(User.email == trader_email)
        ).scalar_one_or_none()
        if trader is None:
            log.error("trader not found: %s", trader_email)
            return 3
        if trader.role != UserRole.TRADER:
            log.error(
                "user %s exists but role is %s, expected trader",
                trader_email, trader.role.value,
            )
            return 3

        # Single encrypted blob reused for every fake account — the
        # FakeBrokerAdapter ignores credentials, but copy_engine still
        # runs the decrypt step, so we need a valid Fernet token.
        empty_creds_blob = encrypt_json({})
        # Cheap shared password hash — these users never sign in via the
        # UI, but the column is NOT NULL.
        shared_pw_hash = bcrypt.hash("fake-load-test-not-for-login")

        # Pre-load existing seeded emails so we skip them in one query
        # instead of catching IntegrityErrors one at a time.
        existing_emails = set(db.execute(
            select(User.email).where(
                User.email.like(f"{EMAIL_PREFIX}%{EMAIL_DOMAIN}")
            )
        ).scalars())
        log.info("found %d already-seeded users", len(existing_emails))

        created = 0
        for i in range(count):
            email = _email_for(i)
            if email in existing_emails:
                continue
            user = User(
                id=uuid.uuid4(),
                email=email,
                password_hash=shared_pw_hash,
                role=UserRole.SUBSCRIBER,
                display_name=f"Load Test {i:04d}",
                is_active=True,
            )
            db.add(user)
            db.flush()  # populate user.id without committing

            db.add(SubscriberSettings(
                user_id=user.id,
                following_trader_id=trader.id,
                copy_enabled=True,
                multiplier=multiplier,
                # retry intentionally NOT enabled — the fake adapter
                # never fails, so retry_pending would never fire.
            ))
            db.add(BrokerAccount(
                id=uuid.uuid4(),
                user_id=user.id,
                broker=BrokerName.FAKE,
                label=f"Fake Broker {i:04d}",
                is_paper=True,
                supports_fractional=True,
                encrypted_credentials=empty_creds_blob,
                connection_status="connected",
                broker_account_number=f"FAKE-{i:04d}",
            ))
            created += 1

        db.commit()
        log.info("created %d new fake subscribers (skipped %d existing)",
                 created, count - created)

        # Bust the trader's subscriber cache so the next fanout sees the
        # new rows immediately instead of waiting for TTL. Best-effort —
        # if Redis is down, the cache will catch up on its own.
        try:
            from app.services import cache as cache_svc
            cache_svc.invalidate_subscribers_for_trader(trader.id)
            log.info("invalidated subscriber cache for trader %s", trader_email)
        except Exception:  # noqa: BLE001
            log.warning("could not invalidate subscriber cache (Redis down?) — "
                        "fanout will pick up new subscribers within %ss",
                        # default TTL from config
                        "60")

    return 0


def cmd_count() -> int:
    """Print how many fake-load-test users + accounts exist right now."""
    with SessionLocal() as db:
        users = db.execute(
            select(User).where(
                User.email.like(f"{EMAIL_PREFIX}%{EMAIL_DOMAIN}")
            )
        ).scalars().all()
        user_ids = [u.id for u in users]
        accounts = 0
        following = 0
        if user_ids:
            accounts = db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.user_id.in_(user_ids),
                    BrokerAccount.broker == BrokerName.FAKE,
                )
            ).all()
            accounts = len(accounts)
            following = db.execute(
                select(SubscriberSettings).where(
                    SubscriberSettings.user_id.in_(user_ids),
                    SubscriberSettings.copy_enabled.is_(True),
                    SubscriberSettings.following_trader_id.is_not(None),
                )
            ).all()
            following = len(following)

    print(f"Seeded users:         {len(users)}")
    print(f"Fake broker accounts: {accounts}")
    print(f"Actively following:   {following}")
    return 0


def cmd_cleanup(trader_email_to_invalidate: Optional[str]) -> int:
    """Delete every fake-load-test user. ON DELETE CASCADE on the FK
    sweeps SubscriberSettings + BrokerAccount + Orders + Notifications
    in one go.

    If you pass --trader-email, also invalidates that trader's
    subscriber cache so the now-deleted users disappear from fanout
    immediately rather than waiting for TTL.
    """
    with SessionLocal() as db:
        # Count first so we can report what was deleted.
        before = db.execute(
            select(User.id).where(
                User.email.like(f"{EMAIL_PREFIX}%{EMAIL_DOMAIN}")
            )
        ).scalars().all()
        if not before:
            log.info("nothing to clean up")
            return 0

        log.info("deleting %d fake-load-test users (cascade will drop "
                 "their broker accounts, subscriber settings, orders, "
                 "notifications)", len(before))
        db.execute(
            delete(User).where(
                User.email.like(f"{EMAIL_PREFIX}%{EMAIL_DOMAIN}")
            )
        )
        db.commit()
        log.info("cleanup done")

    if trader_email_to_invalidate:
        with SessionLocal() as db:
            trader = db.execute(
                select(User).where(User.email == trader_email_to_invalidate)
            ).scalar_one_or_none()
            if trader is not None:
                try:
                    from app.services import cache as cache_svc
                    cache_svc.invalidate_subscribers_for_trader(trader.id)
                    log.info("invalidated subscriber cache for trader %s",
                             trader_email_to_invalidate)
                except Exception:  # noqa: BLE001
                    log.warning("could not invalidate cache; will catch up on TTL")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s :: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Seed / count / clean up fake subscribers for load testing."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_seed = sub.add_parser("seed", help="Create N fake subscribers following a trader.")
    p_seed.add_argument("--count", type=int, default=100,
                        help="How many fake subscribers to create. Default 100.")
    p_seed.add_argument("--trader-email", required=True,
                        help="Email of the trader these subscribers should follow.")
    p_seed.add_argument("--multiplier", type=Decimal, default=Decimal("1.0"),
                        help="Multiplier each fake subscriber uses. Default 1.0.")

    sub.add_parser("count", help="Print how many seeded users exist.")

    p_clean = sub.add_parser("cleanup", help="Delete every seeded user (cascades).")
    p_clean.add_argument("--trader-email", default=None,
                         help="If supplied, invalidate this trader's subscriber cache after delete.")

    args = parser.parse_args()
    if args.cmd == "seed":
        return cmd_seed(args.count, args.trader_email, args.multiplier)
    if args.cmd == "count":
        return cmd_count()
    if args.cmd == "cleanup":
        return cmd_cleanup(args.trader_email)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
