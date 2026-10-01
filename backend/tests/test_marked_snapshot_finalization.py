"""Only FINALIZED (eod) broker marked snapshots are used as historical P&L.

An intraday capture (a mid-session Day's P&L) must never be presented as that
day's settled figure — the gaurav Sept 18/21 stale-intraday case. Legacy rows
default to snapshot_type='intraday', so they're excluded until a real post-close
sweep writes an 'eod' row.

Real in-memory SQLite against the actual ORM.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.daily_realized_pnl_snapshot import DailyRealizedPnlSnapshot
from app.services.pnl import frozen_marked_by_day


def _session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    DailyRealizedPnlSnapshot.__table__.create(eng)
    return Session(eng)


def _row(db, uid, day, marked, snapshot_type, *, source="marked", hidden=False, pct=None):
    db.add(DailyRealizedPnlSnapshot(
        id=uuid.uuid4(), user_id=uid, day=day, realized_pnl=Decimal(str(marked)),
        trade_count=0, source=source, snapshot_type=snapshot_type, hidden=hidden, pct=pct,
    ))
    db.flush()


# C + D + E: intraday excluded, eod included, no-snapshot day absent.
def test_only_eod_marked_used_as_historical():
    db = _session()
    u = uuid.uuid4()
    _row(db, u, date(2026, 9, 21), "107.74", "intraday")  # stale mid-day capture
    _row(db, u, date(2026, 9, 22), "815.79", "eod")       # finalized
    res = frozen_marked_by_day(db, u, date(2026, 9, 1), date(2026, 9, 30))
    assert date(2026, 9, 21) not in res, "intraday capture must not be historical"
    assert res[date(2026, 9, 22)][0] == Decimal("815.79")
    # A day with no snapshot at all is simply absent (caller marks it estimated).
    assert date(2026, 9, 23) not in res


def test_realized_source_rows_are_not_marked():
    db = _session()
    u = uuid.uuid4()
    _row(db, u, date(2026, 9, 22), "500", "eod", source="broker_activities")
    res = frozen_marked_by_day(db, u, date(2026, 9, 1), date(2026, 9, 30))
    assert res == {}, "only source='marked' rows are marked values"


def test_hidden_marked_excluded():
    db = _session()
    u = uuid.uuid4()
    _row(db, u, date(2026, 9, 22), "815.79", "eod", hidden=True)
    res = frozen_marked_by_day(db, u, date(2026, 9, 1), date(2026, 9, 30))
    assert res == {}


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("ok", _name)
    print("all passed")
