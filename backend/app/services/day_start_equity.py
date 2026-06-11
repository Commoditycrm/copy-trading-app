"""Day-start equity helper — broker-agnostic baseline for todays_pl.

When the broker doesn't expose a reliable yesterday's-close / day-start
balance (e.g. SnapTrade-routed Alpaca paper), ``pnl_poller`` calls
:func:`get_or_record` with the current equity. The first call on a
given UTC date INSERTs a new row; every subsequent call that day
SELECTs the existing row. The returned value becomes the
``beginning_day_balance`` used to compute ``todays_pl = equity -
beginning_day_balance`` and all percent-based kill switches.

See app/models/daily_equity_snapshot.py for the rationale.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.daily_equity_snapshot import DailyEquitySnapshot

log = logging.getLogger(__name__)


def _today_utc() -> date:
    """Today's UTC date — extracted for testability."""
    return datetime.now(timezone.utc).date()


def get_or_record(
    db: Session,
    broker_account_id: uuid.UUID,
    current_equity: Decimal,
    *,
    utc_date: date | None = None,
) -> Decimal:
    """Return the recorded day-start equity for ``broker_account_id``
    on today UTC, recording ``current_equity`` if no row exists yet.

    ``utc_date`` override is for tests; production passes None and gets
    "today UTC".

    Idempotent across concurrent callers (pnl_poller's multiple ticks +
    any future webhook handler): the ``(broker_account_id, utc_date)``
    unique constraint serializes inserts, and a racing duplicate INSERT
    raises ``IntegrityError`` which we catch and re-SELECT. Caller
    commits the session.
    """
    target_date = utc_date or _today_utc()

    existing_equity = _select_equity(db, broker_account_id, target_date)
    if existing_equity is not None:
        return existing_equity

    # No row for today yet — record current_equity as the day-start.
    row = DailyEquitySnapshot(
        broker_account_id=broker_account_id,
        utc_date=target_date,
        equity=current_equity,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # Another writer beat us to it. Roll back the SAVEPOINT (flush
        # auto-creates one inside an active transaction) and re-read
        # the winner's value. This is the standard "insert-or-select"
        # idiom for unique-constrained tables.
        db.rollback()
        winner = _select_equity(db, broker_account_id, target_date)
        if winner is not None:
            return winner
        # Should be unreachable — IntegrityError means a row exists.
        # If we still can't see it, fall back to returning the caller's
        # equity so todays_pl computes as 0 instead of crashing.
        log.warning(
            "day_start_equity: race-recovery select returned None for "
            "account=%s date=%s — falling back to current equity",
            broker_account_id, target_date,
        )
        return current_equity

    log.info(
        "day_start_equity: snapshotted %s for account=%s on %s",
        current_equity, broker_account_id, target_date,
    )
    return current_equity


def _select_equity(
    db: Session, broker_account_id: uuid.UUID, utc_date: date,
) -> Decimal | None:
    return db.execute(
        select(DailyEquitySnapshot.equity).where(
            DailyEquitySnapshot.broker_account_id == broker_account_id,
            DailyEquitySnapshot.utc_date == utc_date,
        )
    ).scalar_one_or_none()


__all__ = ["get_or_record"]
