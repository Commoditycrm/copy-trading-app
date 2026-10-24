"""Echo/duplicate safety under concurrency, restarts, backfill and retries.

Real Postgres + Redis (the advisory locks and app-originated markers are the
things under test), real commits — each test cleans up the users it made. Only
the broker edge and the fanout itself are stubbed.
"""
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import delete, or_, select

from app.brokers.alpaca import AlpacaAdapter
from app.brokers.base import BrokerOrderResult
from app.database import SessionLocal
from app.models.audit_log import AuditLog
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import Fill, InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole
from app.services import (
    fills_sync, listeners, order_intent, order_retry, retry_scheduler, shared_accounts,
    trade_listener,
)


# ── committed fixtures + cleanup ──────────────────────────────────────────


@pytest.fixture
def world():
    made: list[uuid.UUID] = []

    def user(role):
        with SessionLocal() as db:
            u = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local", password_hash="x", role=role)
            db.add(u)
            db.commit()
            made.append(u.id)
            return u.id

    def acct(user_id, number, broker=BrokerName.ALPACA):
        with SessionLocal() as db:
            a = BrokerAccount(id=uuid.uuid4(), user_id=user_id, broker=broker, label="t",
                              encrypted_credentials="x", broker_account_number=number,
                              created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
            db.add(a)
            db.commit()
            return a.id

    def order(user_id, acct_id, *, parent=None, broker_id=None, oid=None):
        with SessionLocal() as db:
            o = Order(id=oid or uuid.uuid4(), user_id=user_id, broker_account_id=acct_id,
                      parent_order_id=parent, instrument_type=InstrumentType.STOCK, symbol="AAPL",
                      side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=Decimal("5"),
                      status=OrderStatus.ACCEPTED, broker_order_id=broker_id)
            db.add(o)
            db.commit()
            return o.id

    yield SimpleNamespace(user=user, acct=acct, order=order, made=made)

    with SessionLocal() as db:
        ids = list(made)
        oids = list(db.execute(select(Order.id).where(Order.user_id.in_(ids))).scalars())
        db.execute(delete(Fill).where(Fill.order_id.in_(oids)))
        db.execute(delete(Order).where(Order.user_id.in_(ids), Order.parent_order_id.isnot(None)))
        db.execute(delete(Order).where(Order.user_id.in_(ids)))
        db.execute(delete(AuditLog).where(AuditLog.actor_user_id.in_(ids)))
        db.execute(delete(SubscriberSettings).where(SubscriberSettings.user_id.in_(ids)))
        db.execute(delete(BrokerAccount).where(BrokerAccount.user_id.in_(ids)))
        db.execute(delete(User).where(User.id.in_(ids)))
        db.commit()


@pytest.fixture
def listener_env(monkeypatch):
    fanned: list = []
    lock = threading.Lock()

    def _fanout(db, order, trader):
        with lock:
            fanned.append(order.id)
        return []

    monkeypatch.setattr(trade_listener, "_main_loop", None)
    monkeypatch.setattr(trade_listener.copy_engine, "fanout", _fanout)
    monkeypatch.setattr(trade_listener.events, "publish", lambda *a, **k: None)
    return fanned


def _event(broker_id, coid, event="new"):
    o = SimpleNamespace(
        id=broker_id, client_order_id=coid, symbol="AAPL", side="buy", order_type="market",
        qty="5", limit_price=None, stop_price=None, filled_qty="0", filled_avg_price=None,
        status="new", replaces=None, submitted_at=datetime.now(timezone.utc),
    )
    return SimpleNamespace(event=event, order=o)


def _roots(trader_id):
    with SessionLocal() as db:
        return db.execute(
            select(Order).where(Order.user_id == trader_id, Order.parent_order_id.is_(None))
        ).scalars().all()


def _concurrently(n, fn):
    gate = threading.Barrier(n)
    errors: list = []

    def run():
        try:
            gate.wait()
            fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors


def _shared_world(world):
    trader = world.user(UserRole.TRADER)
    sub = world.user(UserRole.SUBSCRIBER)
    t_acct = world.acct(trader, "PA-SHARED")
    s_acct = world.acct(sub, "PA-SHARED")
    parent = world.order(trader, t_acct, broker_id=f"P-{uuid.uuid4()}")
    return trader, sub, t_acct, s_acct, parent


# ── multiple workers / duplicate events ───────────────────────────────────


def test_two_workers_on_the_same_echo_insert_nothing(world, listener_env):
    trader, sub, t_acct, s_acct, parent = _shared_world(world)
    mirror_broker_id = str(uuid.uuid4())
    mirror = world.order(sub, s_acct, parent=parent, broker_id=mirror_broker_id)
    before = len(_roots(trader))
    ev = _event(mirror_broker_id, str(mirror), "accepted")

    _concurrently(4, lambda: trade_listener._persist_and_fanout(trader, t_acct, ev, datetime.now(timezone.utc)))

    assert len(_roots(trader)) == before
    assert listener_env == []


def test_two_workers_on_one_genuine_order_insert_and_fan_out_once(world, listener_env):
    trader = world.user(UserRole.TRADER)
    t_acct = world.acct(trader, "PA-T")
    broker_id = str(uuid.uuid4())
    ev = _event(broker_id, str(uuid.uuid4()), "accepted")

    _concurrently(4, lambda: trade_listener._persist_and_fanout(trader, t_acct, ev, datetime.now(timezone.utc)))

    rows = [o for o in _roots(trader) if o.broker_order_id == broker_id]
    assert len(rows) == 1
    assert listener_env == [rows[0].id]


def test_redelivered_events_for_a_genuine_order_fan_out_once(world, listener_env):
    trader = world.user(UserRole.TRADER)
    t_acct = world.acct(trader, "PA-T")
    broker_id = str(uuid.uuid4())
    coid = str(uuid.uuid4())
    for ev in ("new", "accepted", "accepted", "partial_fill", "fill", "fill"):
        trade_listener._persist_and_fanout(trader, t_acct, _event(broker_id, coid, ev), datetime.now(timezone.utc))
    assert len([o for o in _roots(trader) if o.broker_order_id == broker_id]) == 1
    assert len(listener_env) == 1


# ── restart: no Redis marker, and before the mirror row commits ───────────


def test_after_a_restart_the_db_still_recognises_a_mirror(world, listener_env, monkeypatch):
    """Redis on QA runs with --save '' --appendonly no, so a restart loses
    every marker. The committed mirror row is enough on its own."""
    monkeypatch.setattr(trade_listener.order_intent, "is_app_originated", lambda oid: False)
    trader, sub, t_acct, s_acct, parent = _shared_world(world)
    mirror_broker_id = str(uuid.uuid4())
    mirror = world.order(sub, s_acct, parent=parent, broker_id=mirror_broker_id)
    before = len(_roots(trader))
    for ev in ("new", "accepted", "fill"):
        trade_listener._persist_and_fanout(trader, t_acct, _event(mirror_broker_id, str(mirror), ev),
                                           datetime.now(timezone.utc))
    assert len(_roots(trader)) == before and listener_env == []


def test_an_echo_that_beats_the_mirror_commit_is_caught_by_the_marker(world, listener_env):
    """Phase 1 marks the mirror ids before the broker call; the row itself only
    commits in Phase 3. A 0.17s echo lands in between — real Redis here."""
    trader = world.user(UserRole.TRADER)
    t_acct = world.acct(trader, "PA-T")
    mirror_id = uuid.uuid4()             # no row yet
    order_intent.mark_app_originated_many([mirror_id])
    before = len(_roots(trader))
    trade_listener._persist_and_fanout(trader, t_acct, _event(str(uuid.uuid4()), str(mirror_id), "accepted"),
                                       datetime.now(timezone.utc))
    assert len(_roots(trader)) == before and listener_env == []


# ── backfill after a restart (fills_sync) ─────────────────────────────────


class _FakeAlpaca(AlpacaAdapter):
    def __init__(self, activities):  # noqa: D107 — skip the SDK client
        self._acts = activities

    def list_recent_activities(self):
        return self._acts


def _fill(order_id, act_id):
    return SimpleNamespace(
        activity_type="FILL", id=act_id, side="buy", symbol="AAPL", qty="5", price="100",
        transaction_time=datetime.now(timezone.utc) - timedelta(minutes=10), order_id=order_id,
    )


def test_backfill_does_not_turn_a_mirror_into_a_trader_order(world, monkeypatch):
    trader, sub, t_acct, s_acct, parent = _shared_world(world)
    mirror_broker_id = str(uuid.uuid4())
    world.order(sub, s_acct, parent=parent, broker_id=mirror_broker_id)
    genuine = str(uuid.uuid4())
    acts = [_fill(mirror_broker_id, f"a-{uuid.uuid4()}"), _fill(genuine, f"a-{uuid.uuid4()}")]
    monkeypatch.setattr(fills_sync, "decrypt_json", lambda blob: {})
    monkeypatch.setattr(fills_sync, "adapter_for", lambda acct, creds: _FakeAlpaca(acts))
    monkeypatch.setattr(fills_sync, "_refresh_open_orders", lambda *a, **k: None)

    with SessionLocal() as db:
        res = fills_sync.sync_account_fills(db, db.get(BrokerAccount, t_acct))
        db.commit()

    ids = {o.broker_order_id for o in _roots(trader)}
    assert mirror_broker_id not in ids        # the echo was skipped...
    assert genuine in ids                     # ...a real external fill still lands
    assert res.orders_added == 1


# ── listener quarantine for pairings that predate the fix ─────────────────


def test_shared_trader_account_gets_no_listener(world, monkeypatch):
    trader = world.user(UserRole.TRADER)
    sub = world.user(UserRole.SUBSCRIBER)
    shared = world.acct(trader, "PA-SHARED")
    world.acct(sub, "pa-shared ")
    own_trader = world.user(UserRole.TRADER)
    own = world.acct(own_trader, "PA-OWN")

    assert shared_accounts.listener_blocked(shared)
    assert not shared_accounts.listener_blocked(own)

    spawned: list = []
    monkeypatch.setattr(trade_listener.asyncio, "run_coroutine_threadsafe",
                        lambda *a, **k: spawned.append(a))
    trade_listener.start_listener(trader, shared)
    assert trader not in trade_listener._tasks and spawned == []


def test_reconcile_stops_a_listener_whose_account_became_shared(world, monkeypatch):
    trader = world.user(UserRole.TRADER)
    sub = world.user(UserRole.SUBSCRIBER)
    shared = world.acct(trader, "PA-SHARED")
    with SessionLocal() as db:
        db.get(BrokerAccount, shared).connection_status = "connected"
        db.commit()
    world.acct(sub, "PA-SHARED")
    started, stopped = [], []
    monkeypatch.setattr(listeners, "_running_listener_trader_ids", lambda: {trader})
    monkeypatch.setattr(listeners, "_start_listener_local", lambda t, a: started.append(t))
    monkeypatch.setattr(listeners, "_stop_listener_local", lambda t: stopped.append(t))
    monkeypatch.setattr(listeners.listener_state, "get_all_statuses", lambda: {})
    monkeypatch.setattr(listeners, "_listened_brokers", lambda: [BrokerName.ALPACA])

    listeners.reconcile_once()

    assert trader in stopped and trader not in started


# ── timeouts and broker retries ───────────────────────────────────────────


def test_inline_timeout_retry_resends_the_same_client_order_id(monkeypatch):
    """Alpaca/Webull reject a second order with a used client_order_id, so this
    inline retry can't double-place there. (SnapTrade sends no id — see report.)"""
    monkeypatch.setattr(order_retry.time, "sleep", lambda s: None)
    seen: list = []

    class _Adapter:
        dedupes_client_order_id = True   # Alpaca/Webull; a non-deduping broker never re-sends

        def place_order(self, req):
            seen.append(req.client_order_id)
            if len(seen) == 1:
                raise TimeoutError("Read timed out")
            return BrokerOrderResult(broker_order_id="B1", status=OrderStatus.ACCEPTED,
                                     submitted_at=datetime.now(timezone.utc),
                                     filled_quantity=Decimal(0), filled_avg_price=None)

    from app.brokers.base import BrokerOrderRequest
    req = BrokerOrderRequest(instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.BUY,
                             order_type=OrderType.MARKET, quantity=Decimal("1"), client_order_id="C-1")
    order_retry.place_order_with_recovery(_Adapter(), req, max_attempts=3)
    assert seen == ["C-1", "C-1"]


def test_scheduled_retry_reuses_the_mirrors_own_id(world, monkeypatch):
    trader = world.user(UserRole.TRADER)
    sub = world.user(UserRole.SUBSCRIBER)
    t_acct = world.acct(trader, "PA-T")
    s_acct = world.acct(sub, "PA-S")
    parent = world.order(trader, t_acct, broker_id="P1")
    child = world.order(sub, s_acct, parent=parent)
    with SessionLocal() as db:
        db.add(SubscriberSettings(user_id=sub, following_trader_id=trader, copy_enabled=True))
        c = db.get(Order, child)
        c.status = OrderStatus.RETRY_PENDING
        c.retry_at = datetime.now(timezone.utc)
        db.commit()

    sent: list = []
    monkeypatch.setattr(retry_scheduler, "_passes_gates", lambda db, child, parent: None)
    monkeypatch.setattr(retry_scheduler, "decrypt_json", lambda blob: {})
    monkeypatch.setattr(retry_scheduler, "adapter_for", lambda acct, creds: object())
    monkeypatch.setattr(retry_scheduler.events, "publish", lambda *a, **k: None)

    def _place(adapter, req, **kw):
        sent.append(req.client_order_id)
        return BrokerOrderResult(broker_order_id="B-RETRY", status=OrderStatus.ACCEPTED,
                                 submitted_at=datetime.now(timezone.utc),
                                 filled_quantity=Decimal(0), filled_avg_price=None)

    monkeypatch.setattr(retry_scheduler, "place_order_with_recovery", _place)
    assert retry_scheduler._retry_one_order(child) == "succeeded"
    assert sent == [str(child)]
    with SessionLocal() as db:
        n = db.execute(select(Order).where(Order.parent_order_id == parent)).scalars().all()
    assert len(n) == 1          # retried in place, not a second mirror row


# ── Webull and SnapTrade listeners ────────────────────────────────────────


@pytest.fixture
def no_fanout(monkeypatch):
    from app.services import copy_engine
    called: list = []
    monkeypatch.setattr(copy_engine, "fanout_threadsafe", lambda *a, **k: called.append(a))
    monkeypatch.setattr(copy_engine, "fanout", lambda *a, **k: called.append(a) or [])
    # a restarted worker: no Redis markers survive (QA Redis has no persistence)
    monkeypatch.setattr(order_intent, "is_app_originated", lambda oid: False)
    return called


def test_webull_listener_ignores_a_mirror_echo(world, no_fanout):
    from app.services import webull_listener
    trader = world.user(UserRole.TRADER)
    sub = world.user(UserRole.SUBSCRIBER)
    t_acct = world.acct(trader, "WB-SHARED", broker=BrokerName.WEBULL)
    s_acct = world.acct(sub, "WB-SHARED", broker=BrokerName.WEBULL)
    parent = world.order(trader, t_acct, broker_id="P-WB")
    mirror_id = uuid.uuid4()
    # Webull rows are stored under OUR client id (32-char hex), not Webull's order_id
    world.order(sub, s_acct, parent=parent, broker_id=mirror_id.hex, oid=mirror_id)
    before = len(_roots(trader))
    payload = {"order_id": "WEBULL-NATIVE-123", "client_order_id": mirror_id.hex,
               "order_status": "SUBMITTED", "symbol": "AAPL", "side": "BUY", "qty": "5"}
    webull_listener._persist_and_fanout(trader, t_acct, {}, payload)
    assert len(_roots(trader)) == before and no_fanout == []


def test_snaptrade_listener_ignores_a_mirror_echo(world, no_fanout):
    from app.services import snaptrade_listener
    trader = world.user(UserRole.TRADER)
    sub = world.user(UserRole.SUBSCRIBER)
    t_acct = world.acct(trader, "ST-SHARED", broker=BrokerName.SNAPTRADE)
    s_acct = world.acct(sub, "ST-SHARED", broker=BrokerName.SNAPTRADE)
    parent = world.order(trader, t_acct, broker_id="P-ST")
    mirror_broker_id = f"BRK-{uuid.uuid4()}"
    world.order(sub, s_acct, parent=parent, broker_id=mirror_broker_id)
    before = len(_roots(trader))
    snaptrade_listener._persist_and_fanout(trader, t_acct, mirror_broker_id, "OPEN", SimpleNamespace())
    assert len(_roots(trader)) == before and no_fanout == []


def test_echo_helper_leaves_unknown_orders_alone(world):
    """The early-return only fires on a known row; a genuine external order on
    any broker still reaches the normal insert path."""
    trader = world.user(UserRole.TRADER)
    t_acct = world.acct(trader, "WB-T", broker=BrokerName.WEBULL)
    with SessionLocal() as db:
        assert not order_intent.is_mirror_echo(db, trader, t_acct, "WEBULL-NATIVE-999", uuid.uuid4())
        assert not order_intent.is_mirror_echo(db, trader, t_acct, "BRK-unknown", None)


def test_subscribers_sharing_an_account_dont_double_record_each_others_fills(world, monkeypatch):
    """Old QA box: 56 broker orders recorded under two or three subscribers who
    share one Alpaca account — each one's P&L counted the others' trades."""
    trader = world.user(UserRole.TRADER)
    a = world.user(UserRole.SUBSCRIBER)
    b = world.user(UserRole.SUBSCRIBER)
    t_acct = world.acct(trader, "PA-T")
    a_acct = world.acct(a, "PA-SUBS")
    b_acct = world.acct(b, "PA-SUBS")
    parent = world.order(trader, t_acct, broker_id="P-X")
    a_mirror = str(uuid.uuid4())
    world.order(a, a_acct, parent=parent, broker_id=a_mirror)
    acts = [_fill(a_mirror, f"a-{uuid.uuid4()}")]
    monkeypatch.setattr(fills_sync, "decrypt_json", lambda blob: {})
    monkeypatch.setattr(fills_sync, "adapter_for", lambda acct, creds: _FakeAlpaca(acts))
    monkeypatch.setattr(fills_sync, "_refresh_open_orders", lambda *x, **k: None)

    with SessionLocal() as db:
        fills_sync.sync_account_fills(db, db.get(BrokerAccount, b_acct))
        db.commit()
        rows = db.execute(select(Order).where(Order.broker_order_id == a_mirror)).scalars().all()
    assert [r.user_id for r in rows] == [a]     # still only A's row; B didn't adopt it
