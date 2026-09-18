"""Which accounts the subscriber fill reconcilers pick up.

Subscribers get no live trade listener — those are trader-only — so these 30s
sweeps are the ONLY thing that turns a working order into a filled one in our DB.
On direct Webull they are the only fill-sync path that exists at all.

The bug: an account only qualified if it held an order with
``parent_order_id IS NOT NULL``. That condition was doing double duty — "this is
a subscriber's account" AND "this account has pending work" — and it is only
true of orders the COPY ENGINE created. Everything else the app places on a
subscriber's account carries a NULL parent:

  * bracket-emulator TP/SL exits      → bracket_parent_id, parent_order_id NULL
  * auto-liquidator / EOD 0DTE closes → no parent at all
  * Sell-All and manual closes        → no parent at all

So an account whose only working orders were those was never selected, and their
fills never synced: rows sat SUBMITTED forever, close-detection (which reads
filled_quantity) mis-fired, and a flattened position still looked open.

Selection is now by the account owner's ROLE, which is the real invariant —
services.listeners starts listeners only for role == TRADER, so everyone else
must be polled. These tests pin both halves: the previously-missed orders now
qualify, and a trader's own account still never does (on Webull that also
protects the ~10 req/30s per-app_key budget the trader's own poller is using).

Real in-memory SQLite (StaticPool). No broker, no network.
"""
import os
import sys
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import alpaca_subscriber_reconciler as alpaca_rec
from app.services import webull_subscriber_reconciler as webull_rec

_SUB = uuid.UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
_TRADER = uuid.UUID("b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e")
_ADMIN = uuid.UUID("c3d4e5f6-a7b8-4c9d-0e1f-2a3b4c5d6e7f")


def _make_session():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in (User, BrokerAccount, Order):
        model.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    for uid, role in ((_SUB, UserRole.SUBSCRIBER),
                      (_TRADER, UserRole.TRADER),
                      (_ADMIN, UserRole.ADMIN)):
        db.add(User(id=uid, email=f"{role.value}@example.com",
                    password_hash="x", role=role, is_active=True))
    db.commit()
    return db


def _account(db, user_id, broker=BrokerName.WEBULL, status="connected") -> uuid.UUID:
    acct = BrokerAccount(
        id=uuid.uuid4(), user_id=user_id, broker=broker, label="acct",
        is_paper=False, supports_fractional=False,
        encrypted_credentials="x", connection_status=status,
    )
    db.add(acct)
    db.commit()
    return acct.id


def _order(db, acct_id, user_id, *, parent=None, bracket_parent=None,
           status=OrderStatus.SUBMITTED, boid="WB1", submitted_at=None):
    o = Order(
        id=uuid.uuid4(), user_id=user_id, broker_account_id=acct_id,
        submitted_at=submitted_at,
        parent_order_id=parent, bracket_parent_id=bracket_parent,
        bracket_leg=("sl" if bracket_parent else None),
        instrument_type=InstrumentType.STOCK, symbol="AAPL",
        side=OrderSide.SELL, order_type=OrderType.MARKET,
        quantity=Decimal("1"), status=status, broker_order_id=boid,
    )
    db.add(o)
    db.commit()
    return o


def _selected(module, db) -> list[uuid.UUID]:
    """Run the module's sweep with the DB it was given and capture which accounts
    it decided to poll, without doing any broker work."""
    polled: list[uuid.UUID] = []

    saved_session = module.SessionLocal
    module.SessionLocal = lambda: _NonClosing(db)
    saved_adapter = module.adapter_for
    saved_decrypt = module.decrypt_json
    import app.services.fills_sync as fs
    saved_refresh = fs._refresh_open_orders

    module.adapter_for = lambda acct, creds: object()
    module.decrypt_json = lambda blob: {}
    fs._refresh_open_orders = lambda db_, acct, adapter: polled.append(acct.id)
    try:
        module._reconcile_once()
    finally:
        module.SessionLocal = saved_session
        module.adapter_for = saved_adapter
        module.decrypt_json = saved_decrypt
        fs._refresh_open_orders = saved_refresh
    return polled


class _NonClosing:
    """`with SessionLocal() as db` would close our shared test session; keep it
    open so successive blocks in one sweep see the same data."""
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *exc):
        return False


# ── the regression: orders the copy engine did not create ───────────────────
def test_bracket_exit_leg_alone_qualifies_the_account():
    """A copied TP/SL exit carries bracket_parent_id, NOT parent_order_id. It was
    invisible to the old gate, so the leg that CLOSED the position never synced."""
    db = _make_session()
    acct = _account(db, _SUB)
    entry = _order(db, acct, _SUB, parent=uuid.uuid4(), status=OrderStatus.FILLED)
    _order(db, acct, _SUB, bracket_parent=entry.id, boid="WB-SL")
    assert _selected(webull_rec, db) == [acct]


def test_auto_liquidator_close_alone_qualifies_the_account():
    """Daily-target liquidation and EOD 0DTE closes have no parent at all."""
    db = _make_session()
    acct = _account(db, _SUB)
    _order(db, acct, _SUB, parent=None, boid="WB-LIQ")
    assert _selected(webull_rec, db) == [acct]


def test_manual_close_alone_qualifies_the_account():
    db = _make_session()
    acct = _account(db, _SUB)
    _order(db, acct, _SUB, parent=None, boid="WB-MANUAL")
    assert _selected(webull_rec, db) == [acct]


def test_copy_mirror_still_qualifies():
    """The case that always worked must keep working."""
    db = _make_session()
    acct = _account(db, _SUB)
    _order(db, acct, _SUB, parent=uuid.uuid4())
    assert _selected(webull_rec, db) == [acct]


# ── the property the old filter was standing in for ─────────────────────────
def test_trader_account_is_never_polled():
    """Traders stream their own fills. Polling them here would double-process and,
    on Webull, eat the same ~10 req/30s app_key budget their poller needs."""
    db = _make_session()
    _account(db, _TRADER)
    trader_acct = db.query(BrokerAccount).filter_by(user_id=_TRADER).one().id
    _order(db, trader_acct, _TRADER, parent=None)
    assert _selected(webull_rec, db) == []


def test_trader_account_with_mirrors_is_still_never_polled():
    """Even a parent_order_id-bearing order on a TRADER's account (a backfill, a
    re-adopted row) must not pull them in."""
    db = _make_session()
    acct = _account(db, _TRADER)
    _order(db, acct, _TRADER, parent=uuid.uuid4())
    assert _selected(webull_rec, db) == []


def test_admin_account_is_polled():
    """An admin has no listener either, so their orders need the sweep — the
    role check is 'not a trader', not 'is a subscriber'."""
    db = _make_session()
    acct = _account(db, _ADMIN)
    _order(db, acct, _ADMIN, parent=None)
    assert _selected(webull_rec, db) == [acct]


# ── the other filters still apply ───────────────────────────────────────────
def test_account_with_no_working_orders_is_skipped():
    """Only accounts with pending work are polled, so API use tracks real work."""
    db = _make_session()
    acct = _account(db, _SUB)
    _order(db, acct, _SUB, parent=None, status=OrderStatus.FILLED)
    assert _selected(webull_rec, db) == []


def test_disconnected_account_is_skipped():
    db = _make_session()
    acct = _account(db, _SUB, status="pending")
    _order(db, acct, _SUB, parent=None)
    assert _selected(webull_rec, db) == []


def test_other_brokers_are_skipped_by_the_webull_sweep():
    db = _make_session()
    acct = _account(db, _SUB, broker=BrokerName.SNAPTRADE)
    _order(db, acct, _SUB, parent=None)
    assert _selected(webull_rec, db) == []


def test_partially_filled_counts_as_working():
    db = _make_session()
    acct = _account(db, _SUB)
    _order(db, acct, _SUB, parent=None, status=OrderStatus.PARTIALLY_FILLED)
    assert _selected(webull_rec, db) == [acct]


# ── the Alpaca twin carries the identical fix ───────────────────────────────
def test_alpaca_twin_picks_up_a_bracket_exit_leg():
    db = _make_session()
    acct = _account(db, _SUB, broker=BrokerName.ALPACA)
    entry = _order(db, acct, _SUB, parent=uuid.uuid4(), status=OrderStatus.FILLED)
    _order(db, acct, _SUB, bracket_parent=entry.id, boid="AL-SL")
    assert _selected(alpaca_rec, db) == [acct]


def test_alpaca_twin_still_skips_traders():
    db = _make_session()
    acct = _account(db, _TRADER, broker=BrokerName.ALPACA)
    _order(db, acct, _TRADER, parent=None)
    assert _selected(alpaca_rec, db) == []


# ── the refresh itself prefers ONE batch call over N per-order reads ────────
# Webull's trade endpoints share ~10 requests / 30s per app_key and this sweep
# runs every 30s, so an account with several working orders would spend its
# whole budget here. _refresh_open_orders swallows per-order failures, so the
# throttled ones simply would not sync — silently, on the only fill-sync path
# direct Webull has.

class _BatchAdapter:
    """Exposes the batch hook plus the per-order fallback, counting both."""
    def __init__(self, snapshot, snapshot_raises=False):
        self._snapshot = snapshot
        self._raises = snapshot_raises
        self.snapshot_calls = 0
        self.get_order_calls: list[str] = []

    def get_orders_snapshot(self):
        self.snapshot_calls += 1
        if self._raises:
            raise RuntimeError("throttled")
        return self._snapshot

    def get_order(self, boid):
        self.get_order_calls.append(boid)
        from app.brokers.base import BrokerOrderResult
        from datetime import datetime, timezone
        return BrokerOrderResult(
            broker_order_id=boid, status=OrderStatus.FILLED,
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=Decimal("1"), filled_avg_price=Decimal("5"),
        )


def _result(boid, status=OrderStatus.FILLED):
    from app.brokers.base import BrokerOrderResult
    from datetime import datetime, timezone
    return BrokerOrderResult(
        broker_order_id=boid, status=status,
        submitted_at=datetime.now(timezone.utc),
        filled_quantity=Decimal("1"), filled_avg_price=Decimal("5"),
    )


def _refresh(db, acct_id, adapter):
    import app.services.fills_sync as fs
    acct = db.get(BrokerAccount, acct_id)
    return fs._refresh_open_orders(db, acct, adapter)


def test_batch_snapshot_replaces_the_per_order_reads():
    db = _make_session()
    acct = _account(db, _SUB)
    for i in range(4):
        _order(db, acct, _SUB, parent=uuid.uuid4(), boid=f"c{i}")
    adapter = _BatchAdapter({f"c{i}": _result(f"c{i}") for i in range(4)})
    _refresh(db, acct, adapter)
    assert adapter.snapshot_calls == 1
    assert adapter.get_order_calls == []      # four orders, one broker call


def test_orders_missing_from_the_snapshot_fall_back_to_get_order():
    """An order from a previous day, or beyond the snapshot's page — covered by
    the per-order read, so the batch is strictly an optimisation."""
    db = _make_session()
    acct = _account(db, _SUB)
    for boid in ("c0", "c1", "yesterday"):
        _order(db, acct, _SUB, parent=uuid.uuid4(), boid=boid)
    adapter = _BatchAdapter({"c0": _result("c0"), "c1": _result("c1")})
    _refresh(db, acct, adapter)
    assert adapter.get_order_calls == ["yesterday"]


def test_a_failed_snapshot_degrades_to_per_order_reads():
    db = _make_session()
    acct = _account(db, _SUB)
    for i in range(3):
        _order(db, acct, _SUB, parent=uuid.uuid4(), boid=f"c{i}")
    adapter = _BatchAdapter({}, snapshot_raises=True)
    _refresh(db, acct, adapter)
    assert sorted(adapter.get_order_calls) == ["c0", "c1", "c2"]


def test_a_single_order_skips_the_batch_call():
    """One call either way, and get_order is the more precise read."""
    db = _make_session()
    acct = _account(db, _SUB)
    _order(db, acct, _SUB, parent=uuid.uuid4(), boid="only")
    adapter = _BatchAdapter({"only": _result("only")})
    _refresh(db, acct, adapter)
    assert adapter.snapshot_calls == 0
    assert adapter.get_order_calls == ["only"]


def test_batch_results_are_applied_to_the_rows():
    db = _make_session()
    acct = _account(db, _SUB)
    for i in range(2):
        _order(db, acct, _SUB, parent=uuid.uuid4(), boid=f"c{i}")
    _refresh(db, acct, _BatchAdapter(
        {f"c{i}": _result(f"c{i}") for i in range(2)}
    ))
    db.commit()
    rows = db.query(Order).filter(Order.broker_account_id == acct).all()
    assert all(r.status == OrderStatus.FILLED for r in rows)
    assert all(r.filled_quantity == Decimal("1") for r in rows)


# ── adaptive cadence ────────────────────────────────────────────────────────
# Webull's budget is ~10 requests / 30s per app_key, SHARED with the order calls
# themselves. A flat 5s sweep claims 6 of those 10, and claims them at exactly
# the moment a trade needs them — an order is "working" precisely while the copy
# engine is placing and closing. Modelled worst case for a contentious close:
# 6 (sweep) + 1.5 (P&L poller) + 5 (position read, place, cancel, re-place,
# re-read) = 12.5, and the calls that lose that race are the ORDER ones.
#
# So the cadence follows the value: a mirror is forced to market or a marketable
# limit, so it fills within seconds of placement and that window is worth
# spending budget on. Anything still working afterwards is a quiet resting limit
# where 30s of lag costs nothing.
from datetime import datetime, timedelta, timezone  # noqa: E402


def _reset_schedule():
    webull_rec._next_due_at.clear()


def _fixed_intervals(fast=5.0, idle=30.0, window=90.0):
    saved = webull_rec._intervals
    webull_rec._intervals = lambda: (fast, idle, window)
    return saved


def test_recent_order_gets_the_fast_cadence():
    _reset_schedule()
    saved = _fixed_intervals()
    try:
        db = _make_session()
        acct = _account(db, _SUB)
        _order(db, acct, _SUB, parent=uuid.uuid4(),
               submitted_at=datetime.now(timezone.utc))
        import time as _t
        before = _t.monotonic()
        assert _selected(webull_rec, db) == [acct]
        gap = webull_rec._next_due_at[acct] - before
        assert 4.0 <= gap <= 6.0, gap          # fast interval
    finally:
        webull_rec._intervals = saved


def test_stale_order_drops_to_the_idle_cadence():
    """A limit that has been resting for ten minutes is not about to surprise
    us; its slots are better left for the copy engine."""
    _reset_schedule()
    saved = _fixed_intervals()
    try:
        db = _make_session()
        acct = _account(db, _SUB)
        _order(db, acct, _SUB, parent=uuid.uuid4(),
               submitted_at=datetime.now(timezone.utc) - timedelta(minutes=10))
        import time as _t
        before = _t.monotonic()
        assert _selected(webull_rec, db) == [acct]
        gap = webull_rec._next_due_at[acct] - before
        assert 29.0 <= gap <= 31.0, gap        # idle interval
    finally:
        webull_rec._intervals = saved


def test_an_account_not_yet_due_is_skipped():
    """The loop ticks at the fast interval, so without this every tick would
    poll every account and the cadence would mean nothing."""
    _reset_schedule()
    saved = _fixed_intervals()
    try:
        db = _make_session()
        acct = _account(db, _SUB)
        _order(db, acct, _SUB, parent=uuid.uuid4(),
               submitted_at=datetime.now(timezone.utc))
        assert _selected(webull_rec, db) == [acct]   # first tick polls
        assert _selected(webull_rec, db) == []       # immediate re-tick does not
    finally:
        webull_rec._intervals = saved


def test_schedule_entry_is_dropped_once_nothing_is_working():
    """_next_due_at is module state on a long-running loop; it must not grow
    unboundedly as accounts come and go."""
    _reset_schedule()
    saved = _fixed_intervals()
    try:
        db = _make_session()
        acct = _account(db, _SUB)
        o = _order(db, acct, _SUB, parent=uuid.uuid4(),
                   submitted_at=datetime.now(timezone.utc))
        _selected(webull_rec, db)
        assert acct in webull_rec._next_due_at
        o.status = OrderStatus.FILLED
        db.commit()
        _selected(webull_rec, db)
        assert acct not in webull_rec._next_due_at
    finally:
        webull_rec._intervals = saved


def test_an_order_with_no_submitted_at_still_schedules():
    """created_at is the fallback — a PENDING row placed but not yet submitted
    must not be treated as undateable and skipped."""
    _reset_schedule()
    saved = _fixed_intervals()
    try:
        db = _make_session()
        acct = _account(db, _SUB)
        _order(db, acct, _SUB, parent=uuid.uuid4(), status=OrderStatus.PENDING)
        assert _selected(webull_rec, db) == [acct]
        assert acct in webull_rec._next_due_at
    finally:
        webull_rec._intervals = saved


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll subscriber-reconciler scope tests passed.")
