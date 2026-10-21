"""One brokerage account, two app users.

When a subscriber's broker row and their trader's broker row are the same
brokerage account (same broker + account number), every mirror lands in the
account the trader's listener watches and comes back as a "new" trader order —
QA 2026-10-21: one trade became 52 trader orders + 361 mirrors in 8 minutes,
and again from 11:16 until the account was switched off. Even without a loop,
the trader's listener reads the other user's own orders (their manual trades,
their auto-liquidations) as the trader's and copies them out — 89 such copies
on the old QA box between 09-08 and 10-19.

So: refuse to create the pairing (connect / activate / follow), never mirror
into it if it already exists, and never listen on a trader account another
user also holds. A blank account number never matches — we can't tell.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.broker_account import BrokerAccount, BrokerName
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole

log = logging.getLogger(__name__)

INACTIVE = "inactive"

# accounts we've already logged as quarantined, so the reconcile tick stays quiet
_quarantined: set[uuid.UUID] = set()


def _norm(num: str | None) -> str:
    return (num or "").strip().upper()


def _keys(db: Session, user_ids, *, live_only: bool) -> set[tuple[BrokerName, str]]:
    ids = list(user_ids)
    if not ids:
        return set()
    q = select(BrokerAccount.broker, BrokerAccount.broker_account_number).where(
        BrokerAccount.user_id.in_(ids),
        BrokerAccount.broker_account_number.isnot(None),
    )
    if live_only:
        q = q.where(BrokerAccount.connection_status != INACTIVE)
    return {(b, _norm(n)) for b, n in db.execute(q).all() if _norm(n)}


def self_mirror_account_ids(
    db: Session, trader_id: uuid.UUID, sub_user_ids: "list[uuid.UUID]",
) -> set[uuid.UUID]:
    """Ids of the subscribers' broker rows that are really one of the trader's
    accounts (any status on the trader side — an inactive row can come back)."""
    if not sub_user_ids:
        return set()
    trader_keys = _keys(db, [trader_id], live_only=False)
    if not trader_keys:
        return set()
    rows = db.execute(
        select(BrokerAccount.id, BrokerAccount.broker, BrokerAccount.broker_account_number).where(
            BrokerAccount.user_id.in_(sub_user_ids),
            BrokerAccount.broker.in_({b for b, _ in trader_keys}),
            BrokerAccount.broker_account_number.isnot(None),
        )
    ).all()
    return {aid for aid, broker, num in rows if (broker, _norm(num)) in trader_keys}


def _counterparts(db: Session, user: User) -> list[uuid.UUID]:
    """Users whose account must never be this user's account: a subscriber's
    trader, or every follower of a trader (paused ones too — they can resume)."""
    if user.role == UserRole.SUBSCRIBER:
        ss = db.get(SubscriberSettings, user.id)
        return [ss.following_trader_id] if ss is not None and ss.following_trader_id else []
    if user.role == UserRole.TRADER:
        return list(db.execute(
            select(SubscriberSettings.user_id).where(SubscriberSettings.following_trader_id == user.id)
        ).scalars())
    return []


def connect_conflict(db: Session, user: User, broker: BrokerName, number: str | None) -> bool:
    """Would connecting/activating (broker, number) for ``user`` put them on the
    same brokerage account as their trader / one of their followers?"""
    key = (broker, _norm(number))
    if not key[1]:
        return False
    return key in _keys(db, _counterparts(db, user), live_only=True)


def follow_conflict(db: Session, subscriber_id: uuid.UUID, trader_id: uuid.UUID) -> bool:
    sub_keys = _keys(db, [subscriber_id], live_only=True)
    return bool(sub_keys and sub_keys & _keys(db, [trader_id], live_only=True))


def shared_with_another_user(db: Session, acct: BrokerAccount) -> bool:
    """True when some other user also has a row for this brokerage account. A
    trader listener on such an account can't tell its owner's orders from the
    other user's, so it must not run."""
    num = _norm(acct.broker_account_number)
    if not num:
        return False
    for other in db.execute(
        select(BrokerAccount.broker_account_number).where(
            BrokerAccount.broker == acct.broker,
            BrokerAccount.user_id != acct.user_id,
            BrokerAccount.broker_account_number.isnot(None),
        )
    ).scalars():
        if _norm(other) == num:
            return True
    return False


def listener_blocked(broker_account_id: uuid.UUID) -> bool:
    """Gate for every listener backend's start_listener. Catches pairings made
    before the connect guard existed. Fails open on a DB error — a listener that
    can't reach the DB can't insert anything anyway."""
    from app.database import SessionLocal  # noqa: PLC0415

    try:
        with SessionLocal() as db:
            acct = db.get(BrokerAccount, broker_account_id)
            blocked = acct is not None and shared_with_another_user(db, acct)
    except Exception:  # noqa: BLE001
        log.warning("shared-account check failed for %s", broker_account_id, exc_info=True)
        return False
    if not blocked:
        _quarantined.discard(broker_account_id)
    elif broker_account_id not in _quarantined:
        _quarantined.add(broker_account_id)
        log.warning(
            "listener NOT started for broker account %s: another user has the same "
            "brokerage account, so its orders would be copied as this trader's",
            broker_account_id,
        )
    return blocked


CONFLICT_MESSAGE = (
    "shared_broker_account — this brokerage account is already connected by "
    "{who}. A trader and their subscriber can't copy through the same account "
    "(every copy would come back as a new trade). Connect a separate account."
)
