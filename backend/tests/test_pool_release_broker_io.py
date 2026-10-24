"""Page loads hand their DB connection back during broker reads.

QA load test after #468 (2026-10-21, 40 concurrent calendar / positions /
sync-fills loads, pool 10+10): 5/200 requests 500'd on QueuePool timeout and
24 connections sat "idle in transaction". The request session (checked out by
current_user) stayed open across every broker HTTP call — positions,
portfolio-history, day P&L, the fills sync — so slow broker reads, not DB
work, held the pool.

Real Postgres (advisory locks, real pool): set TEST_PG_URL, e.g.
postgresql+psycopg://pg:pg@localhost:55432/pg. Skipped otherwise.
"""
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  (every table for create_all)
from app.api import positions as positions_api
from app.api import trades as trades_api
from app.brokers.alpaca import AlpacaAdapter
from app.models.base import Base
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import Fill, Order
from app.models.user import User, UserRole
from app.services import events, market_hours
from app.services import fills_sync as fs
from app.services import redis_client

_PG_URL = os.environ.get("TEST_PG_URL")
pytestmark = pytest.mark.skipif(not _PG_URL, reason="TEST_PG_URL not set")

_USER = uuid.UUID("b8c2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
_BROKER_S = 0.6     # each fake broker read
_POOL_TIMEOUT = 1   # shorter than a page load's broker time


class _FakeRedis:
    def __init__(self):
        self._d: dict = {}
        self._lock = threading.Lock()

    def get(self, k):
        return self._d.get(k)

    def set(self, k, v, nx=False, ex=None):
        with self._lock:
            if nx and k in self._d:
                return None
            self._d[k] = v
            return True

    def delete(self, *ks):
        for k in ks:
            self._d.pop(k, None)

    def eval(self, _script, _n, key, token):
        with self._lock:
            if self._d.get(key) == token:
                del self._d[key]
                return 1
            return 0


class _SlowAlpaca(AlpacaAdapter):
    """Every broker read sleeps; no network."""

    def __init__(self, activities=(), barrier=None):  # noqa: D107  (no super: no SDK clients)
        self.activities = list(activities)
        self.barrier = barrier
        self.activity_reads = 0
        self.pnl_s = _BROKER_S

    def get_positions(self, *, cached_ok=False, fresh=False):
        time.sleep(_BROKER_S)
        return []

    def get_pnl_snapshot(self):
        time.sleep(self.pnl_s)
        return {"todays_pl": "12.5", "beginning_day_balance": "1000"}

    def marked_pnl_by_day(self, from_, to, tz_name=None):
        time.sleep(_BROKER_S)
        return {}

    def get_order(self, broker_order_id):
        time.sleep(_BROKER_S)
        raise LookupError(broker_order_id)

    def list_recent_activities(self):
        self.activity_reads += 1
        time.sleep(_BROKER_S)
        if self.barrier is not None:
            self.barrier.wait(5)
        return list(self.activities)


@pytest.fixture
def env(monkeypatch):
    schema = f"pool_io_{uuid.uuid4().hex[:8]}"
    admin = create_engine(_PG_URL)
    with admin.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
    eng = create_engine(
        _PG_URL, pool_size=2, max_overflow=0, pool_timeout=_POOL_TIMEOUT,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    # Table by table: a model whose server_default disagrees with its enum
    # (subscriber_settings.retry_interval) can't be created from the models,
    # and none of these pages read it.
    for table in Base.metadata.sorted_tables:
        try:
            table.create(eng)
        except DBAPIError:
            pass
    Sess = sessionmaker(bind=eng)
    with Sess() as s:
        s.add(User(id=_USER, email="t@example.com", password_hash="x",
                   role=UserRole.TRADER, is_active=True))
        s.add(BrokerAccount(id=uuid.uuid4(), user_id=_USER, broker=BrokerName.ALPACA,
                            label="a", encrypted_credentials="x"))
        s.commit()

    adapter = _SlowAlpaca()
    use = lambda *_a, **_k: adapter  # noqa: E731
    for mod in (positions_api, trades_api, fs):
        monkeypatch.setattr(mod, "adapter_for", use)
        monkeypatch.setattr(mod, "decrypt_json", lambda _c: {})
    import app.brokers as brokers_pkg
    import app.services.crypto as crypto
    monkeypatch.setattr(brokers_pkg, "adapter_for", use)       # pnl imports it locally
    monkeypatch.setattr(crypto, "decrypt_json", lambda _c: {})
    fake_redis = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_sync_redis", lambda: fake_redis)
    monkeypatch.setattr(events, "publish", lambda *a, **k: None)

    yield SimpleNamespace(eng=eng, Sess=Sess, adapter=adapter, redis=fake_redis,
                          monkeypatch=monkeypatch)
    eng.dispose()
    with admin.begin() as c:
        c.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    admin.dispose()


def _concurrently(n, fn):
    errors, results = [], []

    def run():
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results, errors


def _page_load(Sess, handler):
    """A request: current_user's query checks out the connection, then the
    handler runs on the same session, as with Depends(get_db)."""
    def go():
        with Sess() as db:
            user = db.get(User, _USER)
            return handler(db, user)
    return go


def test_positions_page_loads_dont_exhaust_the_pool(env):
    load = _page_load(env.Sess, lambda db, user: positions_api.list_positions(
        db=db, user=user, detail=True, fresh=False))
    results, errors = _concurrently(6, load)
    assert errors == []
    assert len(results) == 6
    assert all(r.unreachable == [] for r in results)


def test_calendar_page_loads_dont_exhaust_the_pool(env):
    today = market_hours.now_et().date()
    load = _page_load(env.Sess, lambda db, user: trades_api.calendar_pnl(
        db=db, user=user, from_=today - timedelta(days=3), to=today, tz=None, user_id=None))
    results, errors = _concurrently(4, load)
    assert errors == []
    assert len(results) == 4
    live = [c for c in results[0] if c.live]
    assert live and live[0].day_pnl == Decimal("12.5")  # broker read made it through


def test_day_pnl_loads_dont_exhaust_the_pool(env):
    # Slower than pool_timeout: the request computing it and the ones waiting
    # on its shared answer used to hold connections throughout.
    env.adapter.pnl_s = 1.5
    load = _page_load(env.Sess, lambda db, user: positions_api.account_day_pnl(db=db, user=user))
    results, errors = _concurrently(6, load)
    assert errors == []
    assert all(r["day_pnl"] == 12.5 for r in results)


def test_sync_holds_no_connection_during_broker_reads(env):
    """While the broker read is in flight the sync's session has no
    connection checked out."""
    seen = []
    real = env.adapter.list_recent_activities

    def observe():
        seen.append(env.eng.pool.checkedout())
        return real()

    env.adapter.list_recent_activities = observe
    with env.Sess() as db:
        db.get(User, _USER)
        res = fs.sync_user_fills(db, _USER)
        db.commit()
    assert seen == [0]
    assert res["per_account"] and "error" not in res["per_account"][0]


def test_concurrent_syncs_run_the_broker_reads_once(env):
    def sync():
        with env.Sess() as db:
            out = fs.sync_user_fills(db, _USER)
            db.commit()
            return out

    results, errors = _concurrently(5, sync)
    assert errors == []
    assert sum(1 for r in results if r.get("in_progress")) == 4
    assert env.adapter.activity_reads == 1


def _external_fill(i):
    return {
        "activity_type": "FILL", "id": f"act-{i}", "side": "buy", "symbol": "AAPL",
        "qty": "1", "price": "100", "order_id": f"ext-{i}",
        "transaction_time": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
    }


def test_concurrent_syncs_without_redis_never_duplicate_fills(env):
    """Redis down: every sync reads the broker, and their writes land at the
    same moment. The write-phase advisory lock still lets one synthesize."""
    n = 4
    env.adapter.activities = [_external_fill(i) for i in range(3)]
    env.adapter.barrier = threading.Barrier(n)   # all reads done before any write
    env.monkeypatch.setattr(fs, "_claim_sync", lambda _u: (True, None))

    def sync():
        with env.Sess() as db:
            out = fs.sync_user_fills(db, _USER)
            db.commit()
            return out

    results, errors = _concurrently(n, sync)
    assert errors == []
    assert env.adapter.activity_reads == n
    with env.Sess() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 3
        assert db.scalar(select(func.count()).select_from(Fill)) == 3
    assert sum(r["orders_added"] for r in results) == 3


def test_a_second_sync_adopts_rows_the_first_wrote(env):
    env.adapter.activities = [_external_fill(i) for i in range(2)]
    for _ in range(2):
        with env.Sess() as db:
            fs.sync_user_fills(db, _USER)
            db.commit()
    with env.Sess() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 2
        assert db.scalar(select(func.count()).select_from(Fill)) == 2


def test_min_interval_skips_a_recently_synced_account(env):
    with env.Sess() as s:
        acct = s.execute(select(BrokerAccount)).scalar_one()
        acct.last_activity_sync_at = datetime.now(timezone.utc) - timedelta(seconds=3)
        s.commit()
    with env.Sess() as s:
        fs.sync_user_fills(s, _USER, min_interval=timedelta(seconds=10))
        s.commit()
    assert env.adapter.activity_reads == 0
    with env.Sess() as s:
        fs.sync_user_fills(s, _USER, min_interval=timedelta(seconds=1))
        s.commit()
    assert env.adapter.activity_reads == 1
