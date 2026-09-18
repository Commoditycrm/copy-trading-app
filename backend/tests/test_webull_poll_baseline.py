"""What the direct-Webull REST poller treats as HISTORY when it starts.

The poller primes a "baseline" of order_ids it must never replay as fresh
signals — otherwise starting the app at noon would mirror the whole morning.
That baseline used to be *every order visible on the first cycle*, which was far
too blunt, because the poller restarts on every worker deploy, crash and listener
reconcile — not once a day. Two kinds of live work were silently swallowed:

  1. **An order we already track that is still WORKING.** Baselining it meant its
     later FILL transition was never processed. Concretely: the trader's limit
     rests, the worker restarts, the limit fills — but we never saw the
     transition, so `force_fill_mirrors_to_market` never fired and every
     subscriber's mirror stayed a resting limit while the trader was filled and
     out. This is the expensive one.

  2. **An order placed DURING the restart.** The worker is down for seconds
     during a deploy; a trade in that window should still reach subscribers.

Classification is now three-way, and the third bucket matters as much as the
first two: an order we already track whose broker state still AGREES with ours
is neither history nor work — it is pre-seeded into the seen-map so the restart
stays quiet. Without that, every restart re-runs the handler over every known
order, and for a still-working order `_persist_and_fanout`'s modify branch would
compare terms and fire a cancel-and-replace across EVERY subscriber mirror.

Real in-memory SQLite (StaticPool). No SDK, no network.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.webull_listener as wl
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType

_TRADER = uuid.UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")


def _make_session():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Order.__table__.create(eng)
    return sessionmaker(bind=eng)()


class _NonClosing:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *exc):
        return False


def _wb_time(dt: datetime) -> str:
    """Webull's REST timestamp form: 'YYYY-MM-DD HH:MM:SS.mmm+0000'."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "+0000"


def _rest_order(oid, *, status="FILLED", placed_ago_s=3600, symbol="AAPL",
                qty="1", filled_qty="1", limit_price=None):
    """One row in the shape list_today_orders returns."""
    placed = datetime.now(timezone.utc) - timedelta(seconds=placed_ago_s)
    return {
        "order_id": oid,
        "client_order_id": f"c-{oid}",
        "account_id": "ACC1",
        "items": [{
            "symbol": symbol, "category": "US_STOCK", "side": "BUY",
            "order_status": status, "qty": qty, "filled_qty": filled_qty,
            "filled_price": "100.00", "order_type": "LIMIT",
            "limit_price": limit_price, "place_time": _wb_time(placed),
        }],
    }


def _store(db, oid, status=OrderStatus.SUBMITTED):
    """A trader order row we already have for this broker_order_id."""
    db.add(Order(
        id=uuid.uuid4(), user_id=_TRADER, broker_account_id=uuid.uuid4(),
        parent_order_id=None, instrument_type=InstrumentType.STOCK, symbol="AAPL",
        side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=Decimal("1"),
        status=status, broker_order_id=oid,
    ))
    db.commit()


def _classify(db, orders):
    saved = wl.SessionLocal
    wl.SessionLocal = lambda: _NonClosing(db)
    try:
        return wl._build_poll_baseline(_TRADER, orders)
    finally:
        wl.SessionLocal = saved


# ── (1) an order we already track is never history ──────────────────────────
def test_tracked_working_order_is_not_baselined():
    """The expensive bug: baselining it lost the later fill transition, so the
    subscribers' mirrors never got swept to market."""
    db = _make_session()
    _store(db, "WB-WORKING", OrderStatus.SUBMITTED)
    baseline, _pre = _classify(db, [_rest_order("WB-WORKING", status="SUBMITTED")])
    assert "WB-WORKING" not in baseline


def test_tracked_order_in_sync_is_preseeded_not_reprocessed():
    """Broker agrees with us → neither history nor work. Pre-seeding its
    fingerprint keeps the restart from re-running the handler (and, for a working
    order, from firing a cancel-and-replace across every mirror)."""
    db = _make_session()
    _store(db, "WB-SYNCED", OrderStatus.SUBMITTED)
    row = _rest_order("WB-SYNCED", status="SUBMITTED")
    baseline, pre = _classify(db, [row])
    assert "WB-SYNCED" not in baseline
    assert pre["WB-SYNCED"] == wl._order_fingerprint(wl._rest_order_to_payload(row))


def test_tracked_order_that_moved_while_we_were_down_is_left_to_sync():
    """We have it as SUBMITTED, the broker says FILLED — the row is stale and
    must be processed on this very cycle, so it is NOT pre-seeded."""
    db = _make_session()
    _store(db, "WB-STALE", OrderStatus.SUBMITTED)
    baseline, pre = _classify(db, [_rest_order("WB-STALE", status="FILLED")])
    assert "WB-STALE" not in baseline
    assert "WB-STALE" not in pre        # not suppressed → handler runs → heals


def test_preseeded_fingerprint_still_lets_a_later_fill_through():
    """The seen-map is a fingerprint, not a mute button: once the order fills,
    the fingerprint differs and the poller processes it."""
    db = _make_session()
    _store(db, "WB-LATER", OrderStatus.SUBMITTED)
    working = _rest_order("WB-LATER", status="SUBMITTED", filled_qty="0")
    _baseline, pre = _classify(db, [working])
    filled = _rest_order("WB-LATER", status="FILLED", filled_qty="1")
    assert pre["WB-LATER"] != wl._order_fingerprint(wl._rest_order_to_payload(filled))


# ── (2) unseen orders: history vs. the restart gap ──────────────────────────
def test_unseen_old_order_is_history():
    """Starting the app at noon must not replay the morning."""
    db = _make_session()
    baseline, _pre = _classify(db, [_rest_order("WB-OLD", placed_ago_s=4 * 3600)])
    assert baseline == {"WB-OLD"}


def test_unseen_order_placed_during_the_restart_is_carried_live():
    """A deploy takes seconds; a trade in that gap should still reach
    subscribers rather than being written off as history."""
    db = _make_session()
    baseline, _pre = _classify(db, [_rest_order("WB-GAP", placed_ago_s=20)])
    assert "WB-GAP" not in baseline


def test_catchup_window_has_an_edge():
    db = _make_session()
    inside = wl._POLL_CATCHUP_WINDOW_S - 30
    outside = wl._POLL_CATCHUP_WINDOW_S + 30
    baseline, _pre = _classify(db, [
        _rest_order("WB-IN", placed_ago_s=inside),
        _rest_order("WB-OUT", placed_ago_s=outside),
    ])
    assert baseline == {"WB-OUT"}


def test_undateable_unseen_order_is_treated_as_history():
    """No parseable place_time → the conservative reading. A missed mirror beats
    replaying an unknown-age trade."""
    db = _make_session()
    row = _rest_order("WB-NODATE")
    row["items"][0]["place_time"] = None
    baseline, _pre = _classify(db, [row])
    assert baseline == {"WB-NODATE"}


# ── failure and edge behaviour ──────────────────────────────────────────────
def test_db_failure_falls_back_to_suppressing_everything():
    """If we can't read our own history we must not guess — suppress, which is
    the old behaviour and the safe direction."""
    class _Boom:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *exc):
            return False

    saved = wl.SessionLocal
    wl.SessionLocal = lambda: _Boom()
    try:
        baseline, pre = wl._build_poll_baseline(
            _TRADER, [_rest_order("A"), _rest_order("B")]
        )
    finally:
        wl.SessionLocal = saved
    assert baseline == {"A", "B"} and pre == {}


def test_empty_first_cycle():
    db = _make_session()
    assert _classify(db, []) == (set(), {})


def test_rows_without_an_order_id_are_ignored():
    db = _make_session()
    baseline, pre = _classify(db, [{"items": [{}]}])
    assert baseline == set() and pre == {}


def test_a_subscriber_mirror_row_does_not_count_as_tracked():
    """The lookup is scoped to the trader's OWN orders (parent_order_id IS NULL).
    A mirror row that happened to carry the same broker id must not make us treat
    the trader's order as already-tracked."""
    db = _make_session()
    db.add(Order(
        id=uuid.uuid4(), user_id=_TRADER, broker_account_id=uuid.uuid4(),
        parent_order_id=uuid.uuid4(),          # a mirror, not the trader's order
        instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=Decimal("1"),
        status=OrderStatus.SUBMITTED, broker_order_id="WB-MIRROR",
    ))
    db.commit()
    baseline, _pre = _classify(db, [_rest_order("WB-MIRROR", placed_ago_s=4 * 3600)])
    assert baseline == {"WB-MIRROR"}       # unseen as a trader order → history


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll webull poll-baseline tests passed.")
