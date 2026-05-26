"""Bulk-onboard real subscribers with Alpaca paper-trading accounts.

Reads a CSV of (email, alpaca_key_id, alpaca_secret) rows. For each row:
  1. Creates a User with role=SUBSCRIBER (skipped if email already exists)
  2. Creates SubscriberSettings linked to a target trader, copy_enabled=True
  3. Creates an Alpaca BrokerAccount with encrypted paper-trading creds

Idempotent. Safe to re-run with the same CSV — existing users / broker
accounts are detected and skipped, only missing rows are inserted.

Distinct from ``seed_fake_subscribers.py``: those route to
FakeBrokerAdapter for load-testing and never reach a real broker. THESE
subscribers connect to Alpaca paper and will receive mirror orders that
hit Alpaca's paper API for real.

Usage
-----
    # On the server (containerised backend):
    docker compose exec backend python scripts/onboard_alpaca_subscribers.py seed \\
        --csv /app/scripts/subscribers_2026-05-26.csv \\
        --trader-email gaurav@example.com \\
        --password 'WelcomeTrade@123'

    # See how many real-Alpaca subscribers exist (vs fake/FAKE adapter ones):
    docker compose exec backend python scripts/onboard_alpaca_subscribers.py \\
        list --trader-email gaurav@example.com

CSV format (with header row)
----------------------------
    email,alpaca_key_id,alpaca_secret
    user1@example.com,PK6ZDEJRP6UE42BNTLZ6527WEY,HV3oV6jUXBtpfPufhmWx3tQK...
    user2@example.com,PKLEY27RIAU3EQ2QE75DYINNO2,AZHLALyFLb6HYuTkFYaABuJi...

Safety
------
- Paper-trading only. The script asserts `is_paper=True` on every account
  it creates. If you somehow point this at a live (AK*) key, the broker
  itself will reject paper-endpoint calls — but we don't validate the
  key with Alpaca's API at insert time. Caller's responsibility to make
  sure the CSV contains paper keys.
- The CSV contains live credentials. Keep it on the server only, never
  commit it to git, and remove it (`rm`) after successful onboarding.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import uuid
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, _BACKEND)

from passlib.hash import bcrypt  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.broker_account import BrokerAccount, BrokerName  # noqa: E402
from app.models.settings import SubscriberSettings  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from app.services.crypto import encrypt_json  # noqa: E402

log = logging.getLogger("onboard_alpaca_subscribers")


# ─── CSV loader ───────────────────────────────────────────────────────────────


def _load_csv(path: str) -> list[dict]:
    """Read the CSV and return a list of dicts. Validates the header and
    that every row has the three required non-empty fields. Trims
    whitespace defensively because spreadsheet exports often pad cells.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"CSV not found: {path}")

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"email", "alpaca_key_id", "alpaca_secret"}
        if not reader.fieldnames or required - set(reader.fieldnames):
            raise ValueError(
                f"CSV header must contain {sorted(required)}; "
                f"got {reader.fieldnames}"
            )
        rows = []
        for i, row in enumerate(reader, start=2):  # +2 = header + 1-indexed
            email = (row.get("email") or "").strip().lower()
            key   = (row.get("alpaca_key_id") or "").strip()
            sec   = (row.get("alpaca_secret") or "").strip()
            if not (email and key and sec):
                raise ValueError(f"row {i}: missing email/key/secret")
            if not key.startswith("PK"):
                # Paper keys start with PK. AK is live trading — we refuse.
                raise ValueError(
                    f"row {i}: key {key!r} doesn't start with PK — refusing "
                    f"live-trading keys"
                )
            rows.append({"email": email, "key": key, "secret": sec})
    return rows


# ─── seed ─────────────────────────────────────────────────────────────────────


def cmd_seed(csv_path: str, trader_email: str, password: str) -> int:
    """Insert (or skip-if-exists) users + settings + broker_accounts for
    every row in the CSV. Returns 0 on success, non-zero on validation
    failures."""
    try:
        rows = _load_csv(csv_path)
    except (FileNotFoundError, ValueError) as exc:
        log.error("CSV load failed: %s", exc)
        return 2

    log.info("loaded %d rows from %s", len(rows), csv_path)

    with SessionLocal() as db:
        trader = db.execute(
            select(User).where(User.email == trader_email.lower())
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

        # Shared password hash — bcrypt is intentionally slow, so we
        # avoid hashing the same string 25 times in a row.
        shared_pw_hash = bcrypt.hash(password)

        # Pre-fetch any existing users matching the CSV emails so we can
        # skip them in O(1) rather than a SELECT per row.
        csv_emails = [r["email"] for r in rows]
        existing_users = {
            u.email: u for u in db.execute(
                select(User).where(User.email.in_(csv_emails))
            ).scalars()
        }
        if existing_users:
            log.info("found %d already-registered emails — will reuse",
                     len(existing_users))

        created_users = 0
        created_settings = 0
        created_brokers = 0
        skipped = 0

        for row in rows:
            email = row["email"]
            user = existing_users.get(email)
            if user is None:
                user = User(
                    id=uuid.uuid4(),
                    email=email,
                    password_hash=shared_pw_hash,
                    role=UserRole.SUBSCRIBER,
                    display_name=email.split("@")[0],
                    is_active=True,
                )
                db.add(user)
                db.flush()  # populate user.id without committing
                created_users += 1
            elif user.role != UserRole.SUBSCRIBER:
                log.warning(
                    "skipping %s: existing user has role %s, not subscriber",
                    email, user.role.value,
                )
                skipped += 1
                continue

            # SubscriberSettings — one row per user, upsert semantics.
            settings = db.get(SubscriberSettings, user.id)
            if settings is None:
                db.add(SubscriberSettings(
                    user_id=user.id,
                    following_trader_id=trader.id,
                    copy_enabled=True,
                    # multiplier defaults to 1.000 per model; explicit
                    # for clarity.
                ))
                created_settings += 1
            else:
                # Existing settings: bring them into the new follow without
                # clobbering an existing custom multiplier or retry policy.
                settings.following_trader_id = trader.id
                settings.copy_enabled = True

            # BrokerAccount — refuse to create a second Alpaca account for
            # the same user; existing one wins (idempotency).
            existing_alpaca = db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.user_id == user.id,
                    BrokerAccount.broker == BrokerName.ALPACA,
                )
            ).scalar_one_or_none()
            if existing_alpaca is None:
                creds_blob = encrypt_json({
                    "api_key": row["key"],
                    "api_secret": row["secret"],
                    "paper": True,
                })
                db.add(BrokerAccount(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    broker=BrokerName.ALPACA,
                    label="Alpaca Paper",
                    is_paper=True,
                    supports_fractional=True,
                    encrypted_credentials=creds_blob,
                    connection_status="connected",
                ))
                created_brokers += 1
            else:
                log.info("%s already has an Alpaca broker — keeping it", email)

        db.commit()
        log.info(
            "done: +%d users, +%d settings, +%d broker accounts, %d skipped",
            created_users, created_settings, created_brokers, skipped,
        )

        # Bust the trader's subscriber cache so the next fanout sees the
        # new rows immediately rather than waiting for the 60s TTL.
        try:
            from app.services import cache as cache_svc
            cache_svc.invalidate_subscribers_for_trader(trader.id)
            log.info("invalidated subscriber cache for trader %s", trader_email)
        except Exception:  # noqa: BLE001
            log.warning("could not invalidate cache; will catch up on TTL")

    return 0


# ─── list ─────────────────────────────────────────────────────────────────────


def cmd_list(trader_email: Optional[str]) -> int:
    """Print the subscribers (real, not FAKE) currently following the
    trader. Useful for verifying the seed worked."""
    with SessionLocal() as db:
        q = select(User, SubscriberSettings, BrokerAccount).join(
            SubscriberSettings, SubscriberSettings.user_id == User.id,
        ).outerjoin(
            BrokerAccount,
            (BrokerAccount.user_id == User.id) &
            (BrokerAccount.broker == BrokerName.ALPACA),
        )
        if trader_email:
            trader = db.execute(
                select(User).where(User.email == trader_email.lower())
            ).scalar_one_or_none()
            if trader is None:
                log.error("trader not found: %s", trader_email)
                return 3
            q = q.where(SubscriberSettings.following_trader_id == trader.id)

        rows = db.execute(q.order_by(User.email)).all()

    if not rows:
        print("(no subscribers found)")
        return 0
    print(f"{'email':40s}  {'copy':5s}  {'mult':5s}  {'broker':10s}  status")
    print("-" * 80)
    for u, s, ba in rows:
        broker = "alpaca" if ba and ba.broker == BrokerName.ALPACA else "(none)"
        status = ba.connection_status if ba else "—"
        print(
            f"{u.email:40s}  "
            f"{'ON' if s.copy_enabled else 'OFF':5s}  "
            f"x{float(s.multiplier):<4g}  "
            f"{broker:10s}  "
            f"{status}"
        )
    print(f"\nTotal: {len(rows)} subscriber(s)")
    return 0


# ─── main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s :: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Onboard real Alpaca-paper subscribers from a CSV.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_seed = sub.add_parser(
        "seed",
        help="Register subscribers and connect their Alpaca paper accounts.",
    )
    p_seed.add_argument("--csv", required=True, help="Path to the CSV file.")
    p_seed.add_argument("--trader-email", required=True,
                        help="Trader these subscribers will follow.")
    p_seed.add_argument("--password", required=True,
                        help="Initial login password for all new subscribers.")

    p_list = sub.add_parser(
        "list",
        help="Show subscribers currently set up (optionally filtered by trader).",
    )
    p_list.add_argument("--trader-email", default=None,
                        help="Filter to subscribers following this trader.")

    args = parser.parse_args()
    if args.cmd == "seed":
        return cmd_seed(args.csv, args.trader_email, args.password)
    if args.cmd == "list":
        return cmd_list(args.trader_email)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
