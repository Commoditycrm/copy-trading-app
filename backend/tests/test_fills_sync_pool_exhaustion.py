"""Page-load fills syncs can't exhaust the connection pool.

QA 2026-10-21: two sync_user_fills holders took the per-user advisory lock and
sat "idle in transaction" for 30+ min. Every positions/calendar load runs the
same sync, so 18 more requests parked on pg_advisory_xact_lock, each holding a
pool connection, until the pool (10+10) ran dry and every API call 500'd. The
holders could not finish: _refresh_open_orders ran the deferred-close hooks
inline, and those open their OWN session — a connection the full pool could
not give, so each hook sat out pool_timeout while still holding the lock.

Fixed by (a) try-lock: a second sync for the same user skips instead of
queueing, and (b) running the hooks after commit, off the caller's thread.

The pool tests need real Postgres (advisory locks): set TEST_PG_URL, e.g.
postgresql+psycopg://pg:pg@localhost:55432/pg. Skipped otherwise.
"""
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.brokers.base import BrokerOrderResult
from app.models.base import Base
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import copy_engine, events
from app.services import fills_sync as fs

_USER = uuid.UUID("a7b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
_PG_URL = os.environ.get("TEST_PG_URL")
pg = pytest.mark.skipif(not _PG_URL, reason="TEST_PG_URL not set")


# ── real Postgres, pool_size=2 ───────────────────────────────────────────────

@pytest.fixture
def pg_sessions():
    """A 2-connection pool (no overflow) on a throwaway schema."""
    schema = f"fills_lock_{uuid.uuid4().hex[:8]}"
    admin = create_engine(_PG_URL)
    with admin.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
    eng = create_engine(
        _PG_URL, pool_size=2, max_overflow=0, pool_timeout=2,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(eng, tables=[User.__table__, BrokerAccount.__table__])
    Sess = sessionmaker(bind=eng)
    with Sess() as s:
        s.add(User(id=_USER, email="t@example.com", password_hash="x",
                   role=UserRole.TRADER, is_active=True))
        s.add(BrokerAccount(id=uuid.uuid4(), user_id=_USER, broker=BrokerName.ALPACA,
                            label="a", encrypted_credentials="x"))
        s.commit()
    yield eng, Sess
    eng.dispose()
    with admin.begin() as c:
        c.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    admin.dispose()


def _slow_sync_needing_second_connection(Sess, holding, go, nested_ok):
    """Stands in for a slow sync whose hooks open their own session."""
    def fake(db, acct):
        holding.set()
        go.wait(10)
        try:
            with Sess() as s2:
                s2.execute(text("SELECT 1"))
            nested_ok.append(True)
        except PoolTimeout:
            nested_ok.append(False)
        return fs.SyncResult(fills_added=0, orders_added=0, activities_seen=0, skipped=0)
    return fake


def _hold_sync(Sess, done):
    with Sess() as db:
        done.append(fs.sync_user_fills(db, _USER))
        db.commit()


@pg
def test_blocking_lock_reproduces_the_pile_up(pg_sessions, monkeypatch):
    """The old pg_advisory_xact_lock: one waiter fills the 2-slot pool and the
    holder can't get the connection it needs to finish."""
    eng, Sess = pg_sessions
    holding, go, nested_ok, done = threading.Event(), threading.Event(), [], []
    monkeypatch.setattr(fs, "sync_account_fills",
                        _slow_sync_needing_second_connection(Sess, holding, go, nested_ok))
    holder = threading.Thread(target=_hold_sync, args=(Sess, done))
    holder.start()
    assert holding.wait(5)

    def old_page_load():
        with Sess() as s:
            s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                      {"k": f"fills_sync:{_USER}"})
            s.commit()

    waiter = threading.Thread(target=old_page_load)
    waiter.start()
    time.sleep(0.5)
    assert eng.pool.checkedout() == 2
    go.set()
    holder.join(10)
    waiter.join(10)
    assert nested_ok == [False]  # holder's own connection timed out


@pg
def test_page_loads_skip_while_a_sync_holds_the_lock(pg_sessions, monkeypatch):
    eng, Sess = pg_sessions
    holding, go, nested_ok, done = threading.Event(), threading.Event(), [], []
    monkeypatch.setattr(fs, "sync_account_fills",
                        _slow_sync_needing_second_connection(Sess, holding, go, nested_ok))
    holder = threading.Thread(target=_hold_sync, args=(Sess, done))
    holder.start()
    assert holding.wait(5)

    results, elapsed = [], []

    def page_load():
        t0 = time.monotonic()
        with Sess() as s:
            results.append(fs.sync_user_fills(s, _USER))
            s.commit()
        elapsed.append(time.monotonic() - t0)

    loads = [threading.Thread(target=page_load) for _ in range(5)]
    for t in loads:
        t.start()
    for t in loads:
        t.join(10)
    assert len(results) == 5
    assert all(r.get("in_progress") for r in results)
    assert max(elapsed) < 1.5
    assert eng.pool.checkedout() == 1  # only the holder

    go.set()
    holder.join(10)
    assert not holder.is_alive()
    assert nested_ok == [True]
    assert done and not done[0].get("in_progress")


@pg
def test_min_interval_skips_a_recently_synced_account(pg_sessions, monkeypatch):
    _, Sess = pg_sessions
    calls = []
    monkeypatch.setattr(fs, "sync_account_fills", lambda db, acct: calls.append(acct.id) or
                        fs.SyncResult(fills_added=0, orders_added=0, activities_seen=0, skipped=0))
    with Sess() as s:
        acct = s.query(BrokerAccount).one()
        acct.last_activity_sync_at = datetime.now(timezone.utc) - timedelta(seconds=3)
        s.commit()
    with Sess() as s:
        fs.sync_user_fills(s, _USER, min_interval=timedelta(seconds=10))
        s.commit()
    assert calls == []
    with Sess() as s:
        fs.sync_user_fills(s, _USER, min_interval=timedelta(seconds=1))
        s.commit()
    assert len(calls) == 1


# ── deferred-close hooks run after commit, not inside the transaction ────────

@pytest.fixture
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    for model in (User, BrokerAccount, Order):
        model.__table__.create(eng)
    s = sessionmaker(bind=eng)()
    s.add(User(id=_USER, email="s@example.com", password_hash="x",
               role=UserRole.SUBSCRIBER, is_active=True))
    s.commit()
    yield s
    s.close()


class _Adapter:
    def __init__(self, status):
        self._status = status

    def get_order(self, boid):
        return BrokerOrderResult(broker_order_id=boid, status=self._status,
                                 submitted_at=datetime.now(timezone.utc),
                                 filled_quantity=Decimal("2"), filled_avg_price=Decimal("1.1"))


def _mirror_entry(db):
    acct = BrokerAccount(id=uuid.uuid4(), user_id=_USER, broker=BrokerName.ALPACA,
                         label="a", encrypted_credentials="x")
    db.add(acct)
    o = Order(id=uuid.uuid4(), user_id=_USER, broker_account_id=acct.id,
              parent_order_id=uuid.uuid4(), instrument_type=InstrumentType.OPTION,
              symbol="SPY", side=OrderSide.BUY, order_type=OrderType.LIMIT,
              quantity=Decimal("2"), status=OrderStatus.ACCEPTED, broker_order_id="E1")
    db.add(o)
    db.commit()
    return acct, o


@pytest.fixture
def hooks(monkeypatch):
    fired, cancelled, ran = [], [], threading.Event()
    monkeypatch.setattr(copy_engine, "fire_deferred_closes_for_entry",
                        lambda e: (fired.append(e.id), ran.set()))
    monkeypatch.setattr(copy_engine, "cancel_deferred_closes_for_entry",
                        lambda e: (cancelled.append(e.id), ran.set()))
    monkeypatch.setattr(events, "publish", lambda *a, **k: None)
    return fired, cancelled, ran


def test_filled_entry_hook_waits_for_commit(db, hooks):
    fired, _, ran = hooks
    acct, entry = _mirror_entry(db)
    fs._refresh_open_orders(db, acct, _Adapter(OrderStatus.FILLED))
    assert not ran.wait(0.2)  # nothing while the transaction is open
    db.commit()
    assert ran.wait(5)
    assert fired == [entry.id]


def test_dead_entry_hook_runs_after_commit(db, hooks):
    _, cancelled, ran = hooks
    acct, entry = _mirror_entry(db)
    fs._refresh_open_orders(db, acct, _Adapter(OrderStatus.CANCELED))
    db.commit()
    assert ran.wait(5)
    assert cancelled == [entry.id]


def test_rollback_drops_the_hooks(db, hooks):
    fired, _, ran = hooks
    acct, _ = _mirror_entry(db)
    fs._refresh_open_orders(db, acct, _Adapter(OrderStatus.FILLED))
    db.rollback()
    db.commit()
    assert not ran.wait(0.3)
    assert fired == []
