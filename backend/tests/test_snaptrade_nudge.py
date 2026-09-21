"""On-placement fill nudge for SnapTrade subscriber mirrors.

WHY THIS EXISTS
---------------
SnapTrade serves CACHED brokerage data. Reading it more often does not make a
fill appear sooner — the fill is not there until SnapTrade re-pulls from the
broker. Prod, 7 days of subscriber mirrors: p50 50s, p90 1337s, max ~5.4 days,
with a smooth decay across the buckets (84 / 68 / 40 / 30 / 20 for
0-30s / 30-120s / 2-10m / 10-60m / >1hr) rather than a step at our 30s sweep.
65% of fills land after any sweep interval could explain.

``force_resync()`` is the one call that makes SnapTrade fetch NOW. Before this
change it was reachable from exactly one place — ``_should_force_resync``, which
filters ``parent_order_id IS NULL`` (trader orders) inside the per-TRADER poll
loop — so no subscriber connection was ever nudged. Every subscriber fill waited
on SnapTrade's own cadence.

WHAT THESE TESTS PIN
--------------------
1. A mirror placement schedules a nudge, and the nudge resyncs BEFORE it reads
   (reading first would just re-read the cache we're asking it to replace).
2. The read is LIGHT — exactly one SnapTrade call, not the sweep's four.
3. Non-SnapTrade and non-connected accounts cost nothing.
4. The token bucket caps what nudging can draw from SnapTrade's SHARED quota.
5. It never invents a fill: with nothing new from the broker, rows are untouched.

No network, no real threads for the stage logic — ``_step`` is driven directly
so the sequencing is deterministic.
"""
import os
import sys
import time
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import snaptrade_nudge as nudge

_SUB = uuid.UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")


class _NonClosing:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *exc):
        return False


def _make_session():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in (User, BrokerAccount, Order):
        model.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    db.add(User(id=_SUB, email="sub@example.com", password_hash="x",
                role=UserRole.SUBSCRIBER, is_active=True))
    db.commit()
    return db


def _account(db, broker=BrokerName.SNAPTRADE, status="connected") -> uuid.UUID:
    acct = BrokerAccount(
        id=uuid.uuid4(), user_id=_SUB, broker=broker, label="acct",
        is_paper=False, supports_fractional=True,
        encrypted_credentials="x", connection_status=status,
    )
    db.add(acct)
    db.commit()
    return acct.id


def _order(db, acct_id, *, status=OrderStatus.SUBMITTED, boid="ST1"):
    o = Order(
        id=uuid.uuid4(), user_id=_SUB, broker_account_id=acct_id,
        parent_order_id=uuid.uuid4(),
        instrument_type=InstrumentType.STOCK, symbol="AAPL",
        side=OrderSide.BUY, order_type=OrderType.MARKET,
        quantity=Decimal("1"), status=status, broker_order_id=boid,
    )
    db.add(o)
    db.commit()
    return o


class _FakeAdapter:
    """Counts every SnapTrade call so a test can assert the exact cost."""
    def __init__(self, resync="ok", activities=None, positions=None):
        self.calls: list[str] = []
        self._resync = resync
        self._activities = activities or []
        self._positions = positions or []

    def force_resync(self):
        self.calls.append("force_resync")
        return self._resync

    def list_recent_activities(self):
        self.calls.append("list_recent_activities")
        return self._activities

    def get_positions(self):
        self.calls.append("get_positions")
        return self._positions

    def get_order(self, broker_order_id):
        self.calls.append("get_order")
        raise AssertionError("light reconcile must not call get_order")


class _Activity:
    """Minimal stand-in for one SnapTrade activities-feed row. Only the fields
    _persist_subscriber_fill reads via _attr."""
    def __init__(self, boid, status="EXECUTED", filled="1", price="10"):
        self.brokerage_order_id = boid
        self.status = status
        self.filled_units = filled
        self.execution_price = price


class _Harness:
    """Point the nudge module's DB + adapter at the test session."""
    def __init__(self, db, adapter):
        self.db, self.adapter = db, adapter
        self._saved = {}

    def __enter__(self):
        import app.database as database
        import app.services.snaptrade_listener as sl
        self._saved["db_session"] = database.SessionLocal
        self._saved["sl_session"] = sl.SessionLocal
        self._saved["adapter_for"] = nudge._adapter_for
        self._saved["load_creds"] = sl._load_creds
        database.SessionLocal = lambda: _NonClosing(self.db)
        sl.SessionLocal = lambda: _NonClosing(self.db)
        nudge._adapter_for = lambda n: self.adapter
        sl._load_creds = lambda u, a: {"snaptrade_user_id": "u"}
        # A fresh bucket + clean throttle memo per test.
        nudge._bucket = nudge._Bucket()
        nudge._last_resync.clear()
        sl._refresh_unsupported.clear()
        return self

    def __exit__(self, *exc):
        import app.database as database
        import app.services.snaptrade_listener as sl
        database.SessionLocal = self._saved["db_session"]
        sl.SessionLocal = self._saved["sl_session"]
        nudge._adapter_for = self._saved["adapter_for"]
        sl._load_creds = self._saved["load_creds"]
        return False


def _nudge_for(acct_id) -> "nudge._Nudge":
    return nudge._Nudge(user_id=_SUB, account_id=acct_id, due_at=time.monotonic())


# ── 1. resync happens FIRST, and the read is deferred ───────────────────────
def test_resync_precedes_the_read_and_the_read_is_deferred():
    """SnapTrade's refresh is ASYNC. Reading in the same step would hit the very
    cache we just asked it to replace, so stage 1 must be resync-only and stage
    2 must be scheduled for later, not run inline."""
    db = _make_session()
    acct = _account(db)
    _order(db, acct)
    adapter = _FakeAdapter()

    with _Harness(db, adapter):
        n = _nudge_for(acct)
        again = nudge._step(n)
        assert adapter.calls == ["force_resync"], adapter.calls
        assert again is True, "must come back for the read"
        assert n.due_at - time.monotonic() > 1.0, "read must be deferred, not inline"

        nudge._step(n)
        assert adapter.calls == ["force_resync", "list_recent_activities"]


# ── 2. the read is LIGHT: one call, not the sweep's four ────────────────────
def test_read_is_one_call_no_get_order_no_get_positions():
    """SnapTrade's quota is ONE shared platform-wide pool, so a per-mirror nudge
    is only affordable if it costs a single call. The full sweep's get_order per
    working order (_refresh_open_orders) and get_positions (_broker_net_map) are
    skipped — _FakeAdapter.get_order raises if that regresses."""
    db = _make_session()
    acct = _account(db)
    o = _order(db, acct)
    # A NON-EMPTY feed matters: an empty one short-circuits before the sweep's
    # _broker_net_map, so an empty-feed test would pass even if light mode
    # forgot to skip get_positions.
    adapter = _FakeAdapter(activities=[_Activity(o.broker_order_id)])

    with _Harness(db, adapter):
        n = _nudge_for(acct)
        n.resync_done = True
        nudge._step(n)

    assert adapter.calls == ["list_recent_activities"], adapter.calls


# ── 3. wrong broker / disconnected costs nothing ────────────────────────────
def test_non_snaptrade_account_makes_no_calls():
    """schedule() callers don't filter by broker — copy_engine fires it for every
    mirror. The account gate is here, so an Alpaca or Webull fanout must not
    touch SnapTrade at all."""
    db = _make_session()
    acct = _account(db, broker=BrokerName.ALPACA)
    _order(db, acct)
    adapter = _FakeAdapter()

    import app.database as database
    import app.services.snaptrade_listener as sl
    saved_db, saved_sl = database.SessionLocal, sl.SessionLocal
    saved_creds = sl._load_creds
    database.SessionLocal = lambda: _NonClosing(db)
    sl.SessionLocal = lambda: _NonClosing(db)
    sl._load_creds = lambda u, a: {"snaptrade_user_id": "u"}
    try:
        assert nudge._step(_nudge_for(acct)) is False
    finally:
        database.SessionLocal, sl.SessionLocal = saved_db, saved_sl
        sl._load_creds = saved_creds
    assert adapter.calls == []


def test_disconnected_account_makes_no_calls():
    db = _make_session()
    acct = _account(db, status="disconnected")
    _order(db, acct)

    import app.database as database
    import app.services.snaptrade_listener as sl
    saved_db, saved_sl = database.SessionLocal, sl.SessionLocal
    saved_creds = sl._load_creds
    database.SessionLocal = lambda: _NonClosing(db)
    sl.SessionLocal = lambda: _NonClosing(db)
    sl._load_creds = lambda u, a: {"snaptrade_user_id": "u"}
    try:
        assert nudge._step(_nudge_for(acct)) is False
    finally:
        database.SessionLocal, sl.SessionLocal = saved_db, saved_sl
        sl._load_creds = saved_creds


# ── 4. the shared-quota guards ──────────────────────────────────────────────
def test_budget_exhaustion_drops_the_nudge_instead_of_burning_quota():
    """The bucket is the hard stop that keeps nudging from eating the quota the
    ORDER calls need. Over budget it must do nothing — degrading to the 30s
    sweep, i.e. today's behaviour, never worse."""
    db = _make_session()
    acct = _account(db)
    _order(db, acct)
    adapter = _FakeAdapter()

    with _Harness(db, adapter):
        nudge._bucket.tokens = 0.0
        nudge._bucket.updated = time.monotonic()
        n = _nudge_for(acct)
        nudge._step(n)                 # resync stage: no token → no call
        assert adapter.calls == []
        assert nudge._step(n) is False  # read stage: no token → give up
        assert adapter.calls == []


def test_resync_is_throttled_per_connection():
    """A fanout can place several mirrors on one account; each must not re-ask
    SnapTrade to refresh the same connection."""
    db = _make_session()
    acct = _account(db)
    _order(db, acct)
    adapter = _FakeAdapter()

    with _Harness(db, adapter):
        nudge._step(_nudge_for(acct))
        assert adapter.calls == ["force_resync"]
        nudge._step(_nudge_for(acct))   # second mirror, same account
        assert adapter.calls == ["force_resync"], "resync must be throttled"


def test_plan_without_manual_refresh_is_remembered_and_not_retried():
    """A real-time SnapTrade plan 403s force_resync (code 1141). Retrying just
    burns 403s — and the READ is still worth doing, because real-time means the
    fill is already there."""
    import app.services.snaptrade_listener as sl
    db = _make_session()
    acct = _account(db)
    _order(db, acct)
    adapter = _FakeAdapter(resync="forbidden")

    with _Harness(db, adapter):
        nudge._step(_nudge_for(acct))
        assert acct in sl._refresh_unsupported
        nudge._last_resync.clear()          # throttle is not what's stopping it
        n2 = _nudge_for(acct)
        nudge._step(n2)
        assert adapter.calls == ["force_resync"], "must not re-attempt the refresh"
        nudge._step(n2)
        assert adapter.calls[-1] == "list_recent_activities", "read still runs"


def test_schedule_dedupes_per_account():
    """21 subscribers is 21 connections; N mirrors on ONE connection is still one
    nudge. Without this a multi-leg fanout would multiply calls on one account."""
    nudge.set_enabled(True)
    try:
        nudge._pending.clear()
        a = uuid.uuid4()
        with nudge._cv:
            nudge._pending[a] = nudge._Nudge(_SUB, a, time.monotonic(), reads_left=0)
        nudge.schedule(_SUB, a)
        assert len(nudge._pending) == 1
        assert nudge._pending[a].reads_left == nudge.MAX_READS, "read budget topped up"
    finally:
        nudge._pending.clear()
        nudge.set_enabled(False)


# ── 5. it never invents a fill ──────────────────────────────────────────────
def test_nothing_from_the_broker_leaves_the_order_untouched():
    """The whole point of doing this instead of auto-filling: when SnapTrade
    still has no fill, the mirror stays exactly as placed. No status change, no
    fabricated quantity."""
    db = _make_session()
    acct = _account(db)
    o = _order(db, acct)
    adapter = _FakeAdapter(activities=[])

    with _Harness(db, adapter):
        n = _nudge_for(acct)
        n.resync_done = True
        nudge._step(n)

    db.refresh(o)
    assert o.status == OrderStatus.SUBMITTED
    assert (o.filled_quantity or 0) == 0


# ── 6. the worker thread actually drains a scheduled nudge ─────────────────
def test_worker_thread_runs_the_full_sequence():
    """End-to-end through the real queue + worker thread: schedule() -> resync
    -> deferred read. The stage tests above drive _step directly, so this is the
    one that proves the plumbing (claim, requeue, notify) works."""
    db = _make_session()
    acct = _account(db)
    _order(db, acct)
    adapter = _FakeAdapter()

    saved = (nudge.READ_DELAY_S, nudge.READ_RETRY_S, nudge.MIN_GAP_S, nudge.MAX_READS)
    with _Harness(db, adapter):
        nudge.READ_DELAY_S = nudge.READ_RETRY_S = nudge.MIN_GAP_S = 0.01
        nudge.MAX_READS = 1
        nudge.set_enabled(True)
        try:
            nudge._pending.clear()
            nudge.schedule(_SUB, acct)
            assert nudge.drain(timeout=10.0), "queue never drained"
            # drain() returns as the last stage is retired; give the worker a
            # moment to finish the call it is already in.
            time.sleep(0.2)
        finally:
            nudge.set_enabled(False)
            (nudge.READ_DELAY_S, nudge.READ_RETRY_S,
             nudge.MIN_GAP_S, nudge.MAX_READS) = saved

    assert adapter.calls == ["force_resync", "list_recent_activities"], adapter.calls


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
