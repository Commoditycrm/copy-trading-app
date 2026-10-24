"""SnapTrade mirrors whose send went unconfirmed, against real Postgres.

SnapTrade takes no client id, so an unconfirmed mirror can only be matched to
a broker order by contract, side and quantity. When that match could belong to
another order of ours — a sibling mirror, a row whose broker id is null or has
changed — it must go to manual review: never adopted, never re-sent.

Needs TEST_PG_URL pointing at a database migrated to head (e.g.
postgresql+psycopg://postgres:test@127.0.0.1:55432/copytest). Each test deletes
the rows it made. Skipped without it.
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
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.brokers.snaptrade import SnapTradeAdapter
from app.models.audit_log import AuditLog
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.settings import RetryInterval, SubscriberSettings
from app.models.user import User, UserRole
from app.services import retry_scheduler
from app.services.order_retry import OUTCOME_UNKNOWN_REASON

_PG_URL = os.environ.get("TEST_PG_URL")
pytestmark = pytest.mark.skipif(not _PG_URL, reason="TEST_PG_URL not set")


def _engine():
    return create_engine(_PG_URL)


@pytest.fixture
def pg():
    eng = _engine()
    made: list[uuid.UUID] = []
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    yield SimpleNamespace(Sess=Sess, made=made)
    with Sess() as db:
        db.execute(delete(AuditLog).where(AuditLog.actor_user_id.in_(made)))
        db.execute(delete(Order).where(Order.user_id.in_(made), Order.parent_order_id.isnot(None)))
        db.execute(delete(Order).where(Order.user_id.in_(made)))
        db.execute(delete(SubscriberSettings).where(SubscriberSettings.user_id.in_(made)))
        db.execute(delete(BrokerAccount).where(BrokerAccount.user_id.in_(made)))
        db.execute(delete(User).where(User.id.in_(made)))
        db.commit()
    eng.dispose()


class _Snap:
    """SnapTradeAdapter over a fake client: the broker's order list is
    whatever `orders` holds when asked; every place call is counted."""

    def __init__(self, orders=()):
        self.orders = list(orders)
        self.place_calls = 0
        self.list_calls = 0
        self.adapter = SnapTradeAdapter(
            {"snaptrade_user_id": "u", "snaptrade_user_secret": "s", "account_id": "acc"}
        )
        self.adapter._client = SimpleNamespace(
            trading=SimpleNamespace(place_force_order=self._place, place_mleg_order=self._place),
            account_information=SimpleNamespace(get_user_account_orders=self._list),
        )

    def _place(self, **kw):
        self.place_calls += 1
        raise AssertionError("an unconfirmed SnapTrade mirror must never be re-sent")

    def _list(self, **kw):
        self.list_calls += 1
        return SimpleNamespace(body=list(self.orders))


def _broker_order(oid, qty=3, side="BUY", symbol="AAPL"):
    return {
        "brokerage_order_id": oid, "status": "EXECUTED", "action": side,
        "total_quantity": str(qty),
        "time_placed": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        "universal_symbol": {"symbol": symbol, "raw_symbol": symbol, "type": {"code": "cs"}},
    }


def _world(pg, n_mirrors=1, *, qty=Decimal(3)):
    """A trader, a subscriber on one SnapTrade account, and n mirrors parked
    after an unconfirmed send (all same contract/side/qty)."""
    with pg.Sess() as db:
        trader = User(id=uuid.uuid4(), email=f"t{uuid.uuid4().hex[:8]}@test.local",
                      password_hash="x", role=UserRole.TRADER)
        sub = User(id=uuid.uuid4(), email=f"s{uuid.uuid4().hex[:8]}@test.local",
                   password_hash="x", role=UserRole.SUBSCRIBER)
        db.add_all([trader, sub])
        db.flush()
        pg.made += [trader.id, sub.id]
        acct = BrokerAccount(id=uuid.uuid4(), user_id=sub.id, broker=BrokerName.SNAPTRADE,
                             label="s", encrypted_credentials="x")
        db.add(acct)
        db.add(SubscriberSettings(user_id=sub.id, following_trader_id=trader.id, copy_enabled=True,
                                  retry_interval_open=RetryInterval.ONE_M, retry_max_attempts=3))
        mirrors = []
        for _ in range(n_mirrors):
            parent = Order(id=uuid.uuid4(), user_id=trader.id, instrument_type=InstrumentType.STOCK,
                           symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
                           quantity=qty, status=OrderStatus.FILLED)
            db.add(parent)
            db.flush()
            child = _mirror(sub.id, acct.id, parent.id, qty)
            db.add(child)
            mirrors.append(child.id)
        db.commit()
        return SimpleNamespace(trader=trader.id, sub=sub.id, acct=acct.id, mirrors=mirrors)


def _mirror(sub_id, acct_id, parent_id, qty, **kw):
    kw.setdefault("status", OrderStatus.RETRY_PENDING)
    kw.setdefault("reject_reason", OUTCOME_UNKNOWN_REASON)
    return Order(id=uuid.uuid4(), user_id=sub_id, broker_account_id=acct_id, parent_order_id=parent_id,
                 instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.BUY,
                 order_type=OrderType.MARKET, quantity=qty, is_closing=False, retry_count=0,
                 retry_at=datetime.now(timezone.utc) - timedelta(seconds=1), **kw)


@pytest.fixture
def scheduler(pg, monkeypatch):
    notified: list = []

    def wire(snap, Sess=None):
        monkeypatch.setattr(retry_scheduler, "SessionLocal", Sess or pg.Sess)
        monkeypatch.setattr(retry_scheduler, "_passes_gates", lambda *a: None)
        monkeypatch.setattr(retry_scheduler, "decrypt_json", lambda c: {})
        monkeypatch.setattr(retry_scheduler, "adapter_for", lambda acct, creds: snap.adapter)
        monkeypatch.setattr(retry_scheduler, "_notify_retry_failed",
                            lambda db, child, *a: notified.append(child.id))
        monkeypatch.setattr(retry_scheduler.events, "publish", lambda *a: None)

    wire.notified = notified
    return wire


def _row(Sess, oid):
    with Sess() as db:
        return db.get(Order, oid)


def _actions(Sess, oid):
    with Sess() as db:
        return list(db.execute(select(AuditLog.action).where(AuditLog.entity_id == str(oid))).scalars())


def _assert_review(Sess, oid):
    o = _row(Sess, oid)
    assert o.status == OrderStatus.REJECTED
    assert o.reject_reason == retry_scheduler.NEEDS_REVIEW_REASON
    assert o.broker_order_id is None
    assert _actions(Sess, oid) == ["copy.retry_needs_review"]


def test_pg_single_unambiguous_match_is_adopted(pg, scheduler):
    w = _world(pg)
    snap = _Snap([_broker_order("ST-1"), _broker_order("ST-other", qty=7)])
    scheduler(snap)
    assert retry_scheduler._retry_one_order(w.mirrors[0]) == "adopted"
    o = _row(pg.Sess, w.mirrors[0])
    assert o.broker_order_id == "ST-1" and o.status == OrderStatus.FILLED
    assert _actions(pg.Sess, w.mirrors[0]) == ["copy.retry_adopted"]
    assert snap.place_calls == 0


def test_pg_simultaneous_identical_mirrors_both_go_to_review(pg, scheduler):
    # Two mirrors, same account/contract/side/qty, both sends unconfirmed.
    # Even with one broker order listed, nothing says whose it is.
    w = _world(pg, n_mirrors=2)
    snap = _Snap([_broker_order("ST-1")])
    scheduler(snap)
    outcomes: dict = {}
    barrier = threading.Barrier(2)

    def run(oid):
        barrier.wait()
        outcomes[oid] = retry_scheduler._retry_one_order(oid)

    threads = [threading.Thread(target=run, args=(m,)) for m in w.mirrors]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert set(outcomes.values()) == {"needs_review"}
    for m in w.mirrors:
        _assert_review(pg.Sess, m)
    assert snap.place_calls == 0


def test_pg_two_matching_broker_orders_go_to_review(pg, scheduler):
    w = _world(pg)
    snap = _Snap([_broker_order("ST-1"), _broker_order("ST-2")])
    scheduler(snap)
    assert retry_scheduler._retry_one_order(w.mirrors[0]) == "needs_review"
    _assert_review(pg.Sess, w.mirrors[0])
    assert snap.place_calls == 0


def test_pg_snaptrade_sibling_with_a_drifted_id_goes_to_review(pg, scheduler):
    # A filled sibling mirror on the same account recorded broker id OLD-ID;
    # SnapTrade now lists that same order as NEW-ID. The one listed match may
    # be the sibling's, so ours is not adopted.
    w = _world(pg)
    with pg.Sess() as db:
        parent = db.get(Order, w.mirrors[0]).parent_order_id
        db.add(_mirror(w.sub, w.acct, parent, Decimal(3), status=OrderStatus.FILLED,
                       reject_reason=None, broker_order_id="OLD-ID"))
        db.commit()
    snap = _Snap([_broker_order("NEW-ID")])
    scheduler(snap)
    assert retry_scheduler._retry_one_order(w.mirrors[0]) == "needs_review"
    _assert_review(pg.Sess, w.mirrors[0])
    assert snap.place_calls == 0


def test_pg_sibling_with_null_broker_id_blocks_adoption(pg, scheduler):
    w = _world(pg)
    with pg.Sess() as db:
        parent = db.get(Order, w.mirrors[0]).parent_order_id
        db.add(_mirror(w.sub, w.acct, parent, Decimal(3), status=OrderStatus.REJECTED,
                       reject_reason="broker timed out"))
        db.commit()
    snap = _Snap([_broker_order("ST-1")])
    scheduler(snap)
    assert retry_scheduler._retry_one_order(w.mirrors[0]) == "needs_review"
    assert snap.place_calls == 0


def test_pg_an_order_on_another_account_or_contract_does_not_block(pg, scheduler):
    w = _world(pg)
    with pg.Sess() as db:
        parent = db.get(Order, w.mirrors[0]).parent_order_id
        db.add(_mirror(w.sub, w.acct, parent, Decimal(5), status=OrderStatus.FILLED,
                       reject_reason=None, broker_order_id="QTY-5"))
        db.commit()
    snap = _Snap([_broker_order("ST-1")])
    scheduler(snap)
    assert retry_scheduler._retry_one_order(w.mirrors[0]) == "adopted"


def test_pg_order_that_lists_late_is_reviewed_not_resent_or_adopted(pg, scheduler):
    # The broker's order list lags: nothing at the check → review. When the
    # order shows up later, the reviewed mirror is not picked up again.
    w = _world(pg)
    snap = _Snap([])
    scheduler(snap)
    assert retry_scheduler._retry_one_order(w.mirrors[0]) == "needs_review"
    snap.orders.append(_broker_order("ST-LATE"))
    assert retry_scheduler._retry_one_order(w.mirrors[0]) == "vanished"
    _assert_review(pg.Sess, w.mirrors[0])
    assert snap.place_calls == 0


def test_pg_lookup_failure_holds_then_reviews(pg, scheduler):
    w = _world(pg)
    snap = _Snap()

    def down(**kw):
        raise RuntimeError("SnapTrade 503")

    snap.adapter._client.account_information.get_user_account_orders = down
    scheduler(snap)
    assert retry_scheduler._retry_one_order(w.mirrors[0]) == "rescheduled"
    o = _row(pg.Sess, w.mirrors[0])
    assert o.status == OrderStatus.RETRY_PENDING and o.reject_reason == OUTCOME_UNKNOWN_REASON
    for _ in range(2):
        with pg.Sess() as db:
            db.get(Order, w.mirrors[0]).retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        out = retry_scheduler._retry_one_order(w.mirrors[0])
    assert out == "needs_review"
    assert snap.place_calls == 0


@pytest.mark.parametrize("listed,siblings,expect", [
    (1, 0, "adopted"),
    (1, 1, "needs_review"),
    (2, 0, "needs_review"),
    (0, 0, "needs_review"),
])
def test_pg_a_fresh_worker_picks_up_the_parked_mirror(pg, scheduler, monkeypatch, listed, siblings, expect):
    # The mirror was parked by a previous process; only the DB row carries the
    # outcome-unknown marker. A new engine + the real poll loop pick it up.
    w = _world(pg, n_mirrors=1 + siblings)
    target = w.mirrors[0]
    for other in w.mirrors[1:]:
        with pg.Sess() as db:
            db.get(Order, other).status = OrderStatus.FILLED   # not due, still a possible owner
            db.commit()
    fresh_engine = _engine()
    snap = _Snap([_broker_order(f"ST-{i + 1}") for i in range(listed)])
    scheduler(snap, Sess=sessionmaker(bind=fresh_engine, expire_on_commit=False))
    monkeypatch.setattr(retry_scheduler.time, "sleep", lambda s: None)
    ticks = iter([False, True])
    retry_scheduler.poll_loop(shutdown_check=lambda: next(ticks))
    fresh_engine.dispose()
    o = _row(pg.Sess, target)
    assert snap.place_calls == 0
    if expect == "adopted":
        assert o.broker_order_id == "ST-1" and _actions(pg.Sess, target) == ["copy.retry_adopted"]
    else:
        _assert_review(pg.Sess, target)
