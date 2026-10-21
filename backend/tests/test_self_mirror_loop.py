"""Copy feedback loop when a subscriber connects the trader's OWN broker account.

QA 2026-10-21: subscriber rev*** had the same Alpaca paper account number as
trader cop***. Each mirror landed in the trader's account, the trader's
trade_updates listener read it as a new trader order (parent_order_id NULL) and
fanned it out again — one POST /api/trades became 52 trader orders and 361
mirrors in eight minutes.

Two guards, tested here against the real Postgres schema (everything rolled
back): the fanout skips a subscriber row that is the trader's account, and the
listener refuses to insert an echo of a mirror as a trader order.
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy.orm import Session

import app.services.copy_engine as ce
from app.database import engine
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole
from app.services import order_intent, trade_listener


@pytest.fixture
def conn():
    c = engine.connect()
    outer = c.begin()
    try:
        yield c
    finally:
        outer.rollback()
        c.close()


def _session(conn):
    # Commits inside the code under test become savepoints of the outer txn.
    return Session(bind=conn, join_transaction_mode="create_savepoint", autoflush=False)


@pytest.fixture
def db(conn):
    s = _session(conn)
    yield s
    s.close()


def _user(db, role):
    u = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local",
             password_hash="x", role=role)
    db.add(u)
    return u


def _acct(db, user_id, number, broker=BrokerName.FAKE):
    a = BrokerAccount(id=uuid.uuid4(), user_id=user_id, broker=broker, label="t",
                      encrypted_credentials="x", broker_account_number=number,
                      created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    db.add(a)
    return a


def _follow(db, sub_id, trader_id):
    db.add(SubscriberSettings(user_id=sub_id, following_trader_id=trader_id,
                              copy_enabled=True, multiplier=Decimal("1.000")))


def _fake_place(item):
    return SimpleNamespace(
        status=OrderStatus.SUBMITTED, broker_order_id="FAKE-" + item.child_order_id.hex[:8],
        submitted_at=datetime.now(timezone.utc), filled_quantity=Decimal("0"),
        filled_avg_price=None, filled_at=None, bracket_legs=[],
    )


@pytest.fixture
def fanout_env(db, monkeypatch):
    """Mock only the external edges: broker call, Redis-backed caches, events."""
    accts_by_user: dict = {}

    async def _subs(_db, trader_id):
        return list(db.query(SubscriberSettings).filter(
            SubscriberSettings.following_trader_id == trader_id,
            SubscriberSettings.copy_enabled.is_(True)))

    async def _accts(_db, user_id):
        return accts_by_user.get(user_id, [])

    async def _threshold():
        return 10_000

    marked: list = []
    monkeypatch.setattr(ce.cache, "get_subscribers_for_trader", _subs)
    monkeypatch.setattr(ce.cache, "get_broker_accounts", _accts)
    monkeypatch.setattr(ce.cache, "decrypt_creds_cached", lambda *a: {})
    monkeypatch.setattr(ce.cache, "invalidate_subscribers_for_trader", lambda *a, **k: None)
    monkeypatch.setattr(ce, "get_fanout_batch_threshold_async", _threshold)
    monkeypatch.setattr(ce, "adapter_for", lambda *a: object())
    monkeypatch.setattr(ce, "_place_mirror_with_conflict_resolve", _fake_place)
    monkeypatch.setattr(ce.events, "publish", lambda *a, **k: None)
    monkeypatch.setattr(ce.order_intent, "mark_app_originated_many",
                        lambda ids: marked.extend(ids))
    return SimpleNamespace(accts_by_user=accts_by_user, marked=marked)


def _trader_buy(db, trader, acct):
    o = Order(id=uuid.uuid4(), user_id=trader.id, broker_account_id=acct.id,
              instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.BUY,
              order_type=OrderType.MARKET, quantity=Decimal("5"), status=OrderStatus.FILLED,
              filled_quantity=Decimal("5"), filled_avg_price=Decimal("100"))
    db.add(o)
    return o


def _by_sub(results):
    out: dict = {}
    for r in results:
        out.setdefault(r.subscriber_user_id, []).append(r.status)
    return out


# ── fanout ────────────────────────────────────────────────────────────────


def test_shared_account_subscriber_is_skipped_others_mirror(db, fanout_env):
    trader = _user(db, UserRole.TRADER)
    shared = _user(db, UserRole.SUBSCRIBER)
    normal = _user(db, UserRole.SUBSCRIBER)
    no_number = _user(db, UserRole.SUBSCRIBER)
    db.flush()
    t_acct = _acct(db, trader.id, "PA3SHARED")
    accts = {
        shared.id: _acct(db, shared.id, " PA3SHARED "),   # same account, stray whitespace
        normal.id: _acct(db, normal.id, "PA3OTHER"),
        no_number.id: _acct(db, no_number.id, None),
    }
    for u in (shared, normal, no_number):
        _follow(db, u.id, trader.id)
        fanout_env.accts_by_user[u.id] = [accts[u.id]]
    order = _trader_buy(db, trader, t_acct)
    db.flush()

    results = asyncio.run(ce.fanout_async(db, order, trader))
    st = _by_sub(results)

    assert st[shared.id] == ["skipped_self_mirror"]
    assert st[normal.id] == ["submitted"]
    assert st[no_number.id] == ["submitted"], "NULL account number must never match"

    children = db.query(Order).filter(Order.parent_order_id == order.id).all()
    assert {c.user_id for c in children} == {normal.id, no_number.id}
    assert not any(c.broker_account_id == accts[shared.id].id for c in children)
    # every placed mirror is marked so a listener can't re-read it pre-commit
    assert set(fanout_env.marked) == {c.id for c in children}


def test_same_number_on_a_different_broker_is_not_a_match(db, fanout_env):
    trader = _user(db, UserRole.TRADER)
    sub = _user(db, UserRole.SUBSCRIBER)
    db.flush()
    t_acct = _acct(db, trader.id, "12345", broker=BrokerName.FAKE)
    s_acct = _acct(db, sub.id, "12345", broker=BrokerName.ALPACA)
    _follow(db, sub.id, trader.id)
    fanout_env.accts_by_user[sub.id] = [s_acct]
    db.flush()

    assert ce.self_mirror_account_ids(db, trader.id, [sub.id]) == set()
    results = asyncio.run(ce.fanout_async(db, _trader_buy(db, trader, t_acct), trader))
    assert _by_sub(results)[sub.id] == ["submitted"]


def test_trader_with_no_account_numbers_skips_nothing(db):
    trader = _user(db, UserRole.TRADER)
    sub = _user(db, UserRole.SUBSCRIBER)
    db.flush()
    _acct(db, trader.id, None)
    _acct(db, sub.id, None)
    _acct(db, sub.id, "")
    db.flush()
    assert ce.self_mirror_account_ids(db, trader.id, [sub.id]) == set()


# ── listener ──────────────────────────────────────────────────────────────


@pytest.fixture
def listener_env(conn, monkeypatch):
    fanned: list = []
    monkeypatch.setattr(trade_listener, "SessionLocal", lambda: _session(conn))
    monkeypatch.setattr(trade_listener, "_main_loop", None)
    monkeypatch.setattr(trade_listener.copy_engine, "fanout",
                        lambda db, order, trader: fanned.append(order.id) or [])
    monkeypatch.setattr(trade_listener.events, "publish", lambda *a, **k: None)
    monkeypatch.setattr(trade_listener.order_intent, "is_app_originated", lambda oid: False)
    return fanned


def _alpaca_event(broker_id, coid, event="new"):
    o = SimpleNamespace(
        id=broker_id, client_order_id=coid, symbol="AAPL", side="buy",
        order_type="market", qty="5", limit_price=None, stop_price=None,
        filled_qty="0", filled_avg_price=None, status="new", replaces=None,
        submitted_at=datetime.now(timezone.utc),
    )
    return SimpleNamespace(event=event, order=o)


def _listener_world(db):
    trader = _user(db, UserRole.TRADER)
    sub = _user(db, UserRole.SUBSCRIBER)
    db.flush()
    t_acct = _acct(db, trader.id, "PA3SHARED", broker=BrokerName.ALPACA)
    s_acct = _acct(db, sub.id, "PA3SHARED", broker=BrokerName.ALPACA)
    parent = _trader_buy(db, trader, t_acct)
    db.flush()
    mirror = Order(id=uuid.uuid4(), user_id=sub.id, broker_account_id=s_acct.id,
                   parent_order_id=parent.id, instrument_type=InstrumentType.STOCK,
                   symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
                   quantity=Decimal("5"), status=OrderStatus.SUBMITTED,
                   broker_order_id=str(uuid.uuid4()))
    db.add(mirror)
    db.commit()
    return trader, t_acct, mirror


def _trader_rows(db, trader):
    return db.query(Order).filter(Order.user_id == trader.id).count()


def test_listener_ignores_echo_of_a_mirror_by_client_order_id(db, listener_env):
    trader, t_acct, mirror = _listener_world(db)
    before = _trader_rows(db, trader)
    # broker id not stored on the mirror yet — only the coid ties it back
    trade_listener._persist_and_fanout(
        trader.id, t_acct.id, _alpaca_event(str(uuid.uuid4()), str(mirror.id)),
        datetime.now(timezone.utc))
    assert _trader_rows(db, trader) == before
    assert listener_env == []


def test_listener_ignores_echo_of_a_mirror_by_broker_order_id(db, listener_env):
    trader, t_acct, mirror = _listener_world(db)
    before = _trader_rows(db, trader)
    for ev in ("new", "accepted", "fill"):
        trade_listener._persist_and_fanout(
            trader.id, t_acct.id, _alpaca_event(mirror.broker_order_id, "not-a-uuid", ev),
            datetime.now(timezone.utc))
    assert _trader_rows(db, trader) == before
    assert listener_env == []


def test_listener_still_mirrors_a_genuine_external_order(db, listener_env):
    trader, t_acct, _mirror = _listener_world(db)
    before = _trader_rows(db, trader)
    broker_id = str(uuid.uuid4())
    trade_listener._persist_and_fanout(
        trader.id, t_acct.id, _alpaca_event(broker_id, str(uuid.uuid4())),
        datetime.now(timezone.utc))
    assert _trader_rows(db, trader) == before + 1
    new = db.query(Order).filter(Order.broker_order_id == broker_id).one()
    assert new.parent_order_id is None and new.user_id == trader.id
    assert listener_env == [new.id]


def test_is_mirror_echo_ignores_the_traders_own_root_order(db):
    trader = _user(db, UserRole.TRADER)
    db.flush()
    acct = _acct(db, trader.id, "X")
    o = _trader_buy(db, trader, acct)
    o.broker_order_id = "B-1"
    db.flush()
    # the listener's own existing-row path handles these; the echo guard must not
    assert not order_intent.is_mirror_echo(db, trader.id, acct.id, "B-1", o.id)


def test_is_mirror_echo_needs_the_same_brokerage_account_for_an_id_match(db):
    """Broker order ids can collide across brokerages; an unrelated user's order
    with the same id in a different account is not an echo."""
    trader = _user(db, UserRole.TRADER)
    other = _user(db, UserRole.SUBSCRIBER)
    db.flush()
    t_acct = _acct(db, trader.id, "ACCT-A")
    o_acct = _acct(db, other.id, "ACCT-B")
    db.flush()
    db.add(Order(id=uuid.uuid4(), user_id=other.id, broker_account_id=o_acct.id,
                 instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.BUY,
                 order_type=OrderType.MARKET, quantity=Decimal("1"),
                 status=OrderStatus.SUBMITTED, broker_order_id="12345"))
    db.flush()
    assert not order_intent.is_mirror_echo(db, trader.id, t_acct.id, "12345", None)
    o_acct.broker_account_number = "ACCT-A"
    db.flush()
    assert order_intent.is_mirror_echo(db, trader.id, t_acct.id, "12345", None)


# ── connect-time warning ──────────────────────────────────────────────────


def test_connect_warns_subscriber_who_connects_their_traders_account(db):
    from app.api.brokers import _shared_with_trader_notice

    trader = _user(db, UserRole.TRADER)
    sub = _user(db, UserRole.SUBSCRIBER)
    db.flush()
    _acct(db, trader.id, "PA3SHARED")
    _follow(db, sub.id, trader.id)
    shared = _acct(db, sub.id, "PA3SHARED")
    own = _acct(db, sub.id, "PA3MINE")
    db.flush()

    assert _shared_with_trader_notice(db, sub, shared, None)
    assert _shared_with_trader_notice(db, sub, own, None) is None
    # the trader connecting their own account is not a warning
    assert _shared_with_trader_notice(db, trader, shared, None) is None
