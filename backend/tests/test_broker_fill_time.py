"""Order History's fill time must be the BROKER's, not ours.

THE BUG
-------
"Time Taken to Filled" computed ``submitted_at -> closed_at``. Every
terminal-status write site sets ``closed_at = datetime.now(timezone.utc)`` — the
moment WE OBSERVED the fill. So the column reported OUR detection latency under
a label that reads as broker speed.

Alpaca looked correct, but only incidentally: it has ``fills`` rows whose
``filled_at`` is the activities feed's ``transaction_time``, and the frontend
preferred those. ``fills_sync.sync_account_fills`` early-returns unless the
adapter is Alpaca, so NO other broker has fill rows to prefer — SnapTrade and
Webull fell through to ``closed_at`` every time.

That mattered beyond cosmetics: it is the instrument used to judge fill latency.
With the two conflated, a fix that cut detection from 22 minutes to 5 seconds
would look identical to one that did nothing, because the broker's own fill time
was never visible.

WHAT THESE PIN
--------------
1. SnapTrade's ``time_executed`` is read (it was being discarded — we parsed
   ``time_placed`` and nothing else).
2. ``time_updated`` substitutes ONLY on executed statuses — never on a cancel or
   reject, where it marks the order's death, not a trade.
3. Webull's timestamps parse from every shape it emits (epoch millis, epoch
   seconds, ISO, space-separated), across BOTH its endpoints, whose field names
   are known to disagree.
4. ``broker_filled_at`` stays NULL when the broker reports nothing — we never
   substitute our own clock into the field whose whole purpose is being the
   broker's.
5. ``closed_at`` remains our detection time, so the gap between the two IS the
   detection lag.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.brokers.base import BrokerOrderResult
from app.brokers.snaptrade import SnapTradeAdapter
from app.brokers.webull import _as_dt, _fill_time
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services.fills_sync import _refresh_open_orders

_USER = uuid.UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
_EXEC = datetime(2026, 9, 18, 14, 30, 5, tzinfo=timezone.utc)


# ── 1-2. SnapTrade: time_executed ───────────────────────────────────────────
class _SnapOrder:
    """Shape of one SnapTrade AccountOrderRecord. The real model carries
    time_placed, time_updated AND time_executed; we only ever read the first."""
    def __init__(self, status="EXECUTED", executed=None, updated=None):
        self.brokerage_order_id = "ST-1"
        self.status = status
        self.time_placed = "2026-09-18T14:30:00Z"
        self.time_executed = executed
        self.time_updated = updated
        self.filled_units = "1"
        self.execution_price = "10"
        self.symbol = {}


def _snap():
    return SnapTradeAdapter.__new__(SnapTradeAdapter)


def test_snaptrade_reads_time_executed():
    res = _snap()._order_to_result(_SnapOrder(executed="2026-09-18T14:30:05Z"))
    assert res.filled_at == _EXEC


def test_snaptrade_falls_back_to_time_updated_when_executed():
    """Not every brokerage populates time_executed. On an EXECUTED order,
    time_updated is the closest defensible stand-in."""
    res = _snap()._order_to_result(
        _SnapOrder(status="EXECUTED", executed=None, updated="2026-09-18T14:30:05Z")
    )
    assert res.filled_at == _EXEC


def test_snaptrade_never_uses_time_updated_on_a_dead_order():
    """On CANCELLED/REJECTED, time_updated is when the order DIED. Recording
    that as a fill time is worse than leaving the field NULL — it would put a
    fabricated execution timestamp on an order that never traded."""
    for status in ("CANCELLED", "REJECTED", "EXPIRED"):
        res = _snap()._order_to_result(
            _SnapOrder(status=status, executed=None, updated="2026-09-18T14:30:05Z")
        )
        assert res.filled_at is None, status


def test_snaptrade_missing_timestamps_leave_it_null():
    res = _snap()._order_to_result(_SnapOrder(executed=None, updated=None))
    assert res.filled_at is None


# ── 3. Webull: every timestamp shape it emits ───────────────────────────────
def test_webull_as_dt_accepts_every_shape():
    """Webull has already been caught spelling one field two ways across its
    two order endpoints (filled_quantity vs filled_qty, and the strike bug
    before that). Its timestamps arrive in as many shapes."""
    assert _as_dt(1789741805000) == _EXEC          # epoch millis
    assert _as_dt("1789741805000") == _EXEC        # epoch millis as string
    assert _as_dt(1789741805) == _EXEC             # epoch seconds
    assert _as_dt("2026-09-18T14:30:05Z") == _EXEC
    assert _as_dt("2026-09-18 14:30:05") == _EXEC  # space separator, naive
    assert _as_dt("2026-09-18T14:30:05+00:00") == _EXEC


def test_webull_as_dt_rejects_junk_rather_than_guessing():
    """This value is stored as the broker's authoritative fill time, so an
    unparseable input must become NULL, never a guess."""
    for junk in (None, "", "not-a-date", 0, -1, "0"):
        assert _as_dt(junk) is None, junk


def test_webull_fill_time_prefers_the_leg_over_the_envelope():
    """The leg is the more specific record; the envelope's update_time can move
    for reasons unrelated to the execution."""
    leg = {"filled_time": "2026-09-18T14:30:05Z"}
    order = {"update_time": "2026-09-18T15:00:00Z"}
    assert _fill_time(leg, order) == _EXEC


def test_webull_fill_time_falls_back_to_the_envelope():
    assert _fill_time({}, {"filled_time": 1789741805000}) == _EXEC


def test_webull_fill_time_covers_both_endpoint_spellings():
    """order/detail and Query Day Orders disagree on field names — that exact
    disagreement caused the zero-fill bug. Check the family, not one name."""
    for key in ("filled_time", "filledTime", "last_filled_time",
                "execution_time", "trade_time", "transaction_time"):
        assert _fill_time({key: "2026-09-18T14:30:05Z"}, {}) == _EXEC, key


# ── 4-5. The write path keeps the two clocks apart ──────────────────────────
def _make_session():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in (User, BrokerAccount, Order):
        model.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    db.add(User(id=_USER, email="s@example.com", password_hash="x",
                role=UserRole.SUBSCRIBER, is_active=True))
    db.commit()
    return db


def _acct(db, broker=BrokerName.WEBULL):
    a = BrokerAccount(
        id=uuid.uuid4(), user_id=_USER, broker=broker, label="a",
        is_paper=False, supports_fractional=False,
        encrypted_credentials="x", connection_status="connected",
    )
    db.add(a)
    db.commit()
    return a


def _order(db, acct):
    o = Order(
        id=uuid.uuid4(), user_id=_USER, broker_account_id=acct.id,
        instrument_type=InstrumentType.STOCK, symbol="AAPL",
        side=OrderSide.BUY, order_type=OrderType.MARKET,
        quantity=Decimal("1"), status=OrderStatus.SUBMITTED,
        broker_order_id="WB-1",
    )
    db.add(o)
    db.commit()
    return o


class _Adapter:
    def __init__(self, filled_at):
        self._filled_at = filled_at

    def get_order(self, boid):
        return BrokerOrderResult(
            broker_order_id=boid, status=OrderStatus.FILLED,
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=Decimal("1"), filled_avg_price=Decimal("10"),
            filled_at=self._filled_at,
        )


def test_refresh_records_the_broker_time_and_our_time_separately():
    """The point of the whole change: closed_at stays OUR clock, so
    broker_filled_at -> closed_at is a measurable detection lag."""
    db = _make_session()
    o = _order(db, (acct := _acct(db)))
    before = datetime.now(timezone.utc)
    _refresh_open_orders(db, acct, _Adapter(_EXEC))
    db.commit()
    db.refresh(o)

    assert o.broker_filled_at is not None
    assert o.broker_filled_at.replace(tzinfo=timezone.utc) == _EXEC
    detected = o.closed_at.replace(tzinfo=timezone.utc) if o.closed_at.tzinfo is None else o.closed_at
    assert detected >= before, "closed_at must remain OUR detection moment"
    assert detected > _EXEC, "the two clocks must not collapse into one"


def test_no_broker_timestamp_leaves_the_column_null():
    """A broker that reports no execution time must NOT get our clock written
    into the field whose entire purpose is to be the broker's. NULL is the
    honest value; the UI falls back to closed_at and marks it approximate."""
    db = _make_session()
    o = _order(db, (acct := _acct(db)))
    _refresh_open_orders(db, acct, _Adapter(None))
    db.commit()
    db.refresh(o)

    assert o.broker_filled_at is None
    assert o.closed_at is not None, "detection still recorded"


def test_broker_time_is_not_overwritten_once_known():
    """A later sweep re-reading the same order must not move the timestamp —
    the first broker-reported value is the execution, later reads are just us
    looking again."""
    db = _make_session()
    o = _order(db, (acct := _acct(db)))
    _refresh_open_orders(db, acct, _Adapter(_EXEC))
    db.commit()
    later = datetime(2026, 9, 18, 16, 0, 0, tzinfo=timezone.utc)
    o.status = OrderStatus.SUBMITTED          # force a re-read
    db.commit()
    _refresh_open_orders(db, acct, _Adapter(later))
    db.commit()
    db.refresh(o)

    assert o.broker_filled_at.replace(tzinfo=timezone.utc) == _EXEC


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
