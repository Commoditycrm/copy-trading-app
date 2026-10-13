"""A Discord subscriber's resting exit orders get re-read from the broker.

A Discord subscriber's exit ladder rests real orders at their broker: the On
Fill stop, take-profit limits, a linked stop. Everything the ladder decides next
(discord_take_profit.settle, discord_stop_orders.reconcile) reads OUR order row,
not the broker, so a take-profit that filled at the broker but still reads
"working" here is a trim that never happened: no new stop for what is left, and
a sell missing from Closed today.

Subscribers have no live trade listener (services.listeners starts those for
TRADERs only), so a per-broker subscriber sweep is the only thing that re-reads
those orders. Alpaca, direct Webull and IBKR select by the owner's role. The
SnapTrade sweep selected only users with a working copy MIRROR
(parent_order_id set), and every Discord ladder order has a NULL parent, so a
SnapTrade subscriber whose working orders were all ladder orders was never
swept.

Real in-memory SQLite (StaticPool). No broker, no network.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.brokers.base import BrokerOrderResult
from app.brokers.snaptrade import SnapTradeAdapter
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import snaptrade_listener as snap
from app.services import webull_subscriber_reconciler as webull_rec

_SUB = uuid.UUID("d1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
_TRADER = uuid.UUID("e2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e")
_FILLED_AT = datetime(2026, 10, 13, 14, 31, 5, tzinfo=timezone.utc)


class _NonClosing:
    """Keep the shared test session open across `with SessionLocal() as db`."""
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *exc):
        return False


@pytest.fixture
def db(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in (User, BrokerAccount, Order):
        model.__table__.create(eng)
    session = sessionmaker(bind=eng)()
    for uid, role in ((_SUB, UserRole.SUBSCRIBER), (_TRADER, UserRole.TRADER)):
        session.add(User(id=uid, email=f"{role.value}@example.com",
                         password_hash="x", role=role, is_active=True))
    session.commit()
    monkeypatch.setattr(snap, "SessionLocal", lambda: _NonClosing(session))
    monkeypatch.setattr(webull_rec, "SessionLocal", lambda: _NonClosing(session))
    webull_rec._next_due_at.clear()
    yield session
    webull_rec._next_due_at.clear()


def _account(db, user_id, broker) -> uuid.UUID:
    acct = BrokerAccount(
        id=uuid.uuid4(), user_id=user_id, broker=broker, label="acct",
        is_paper=True, supports_fractional=False,
        encrypted_credentials="x", connection_status="connected",
    )
    db.add(acct)
    db.commit()
    return acct.id


def _ladder_order(db, acct_id, user_id, *, boid, order_type=OrderType.LIMIT,
                  status=OrderStatus.ACCEPTED, parent=None):
    """A Discord ladder exit as pnl_poller's placers write it: a closing SELL
    with no parent_order_id and no bracket leg."""
    o = Order(
        id=uuid.uuid4(), user_id=user_id, broker_account_id=acct_id,
        parent_order_id=parent,
        instrument_type=InstrumentType.OPTION, symbol="SPY",
        option_strike=Decimal("580"), option_right="put",
        side=OrderSide.SELL, order_type=order_type,
        limit_price=Decimal("1.40") if order_type == OrderType.LIMIT else None,
        stop_price=Decimal("0.80") if order_type == OrderType.STOP else None,
        quantity=Decimal("3"), status=status, broker_order_id=boid,
        is_closing=True,
        submitted_at=datetime(2026, 10, 13, 13, 45, tzinfo=timezone.utc),
    )
    db.add(o)
    db.commit()
    return o


# ── SnapTrade: which accounts the subscriber sweep picks up ─────────────────

def _snap_swept(monkeypatch) -> list[uuid.UUID]:
    swept: list[uuid.UUID] = []
    monkeypatch.setattr(
        snap, "_reconcile_one_subscriber_account",
        lambda user_id, acct_id, **_: swept.append(acct_id),
    )
    snap._reconcile_subscriber_fills_once()
    return swept


def test_snaptrade_subscriber_with_only_a_resting_take_profit_is_swept(db, monkeypatch):
    """The gap: no mirror, just the ladder's take-profit. Never swept before."""
    acct = _account(db, _SUB, BrokerName.SNAPTRADE)
    _ladder_order(db, acct, _SUB, boid="ST-TP")
    assert _snap_swept(monkeypatch) == [acct]


def test_snaptrade_subscriber_with_only_a_resting_stop_is_swept(db, monkeypatch):
    acct = _account(db, _SUB, BrokerName.SNAPTRADE)
    _ladder_order(db, acct, _SUB, boid="ST-SL", order_type=OrderType.STOP)
    assert _snap_swept(monkeypatch) == [acct]


def test_snaptrade_copy_mirror_still_sweeps(db, monkeypatch):
    """The case that always worked keeps working."""
    acct = _account(db, _SUB, BrokerName.SNAPTRADE)
    _ladder_order(db, acct, _SUB, boid="ST-M", parent=uuid.uuid4())
    assert _snap_swept(monkeypatch) == [acct]


def test_snaptrade_idle_subscriber_is_not_swept(db, monkeypatch):
    """No working order, no broker calls: everything already terminal."""
    acct = _account(db, _SUB, BrokerName.SNAPTRADE)
    _ladder_order(db, acct, _SUB, boid="ST-DONE", status=OrderStatus.FILLED)
    assert _snap_swept(monkeypatch) == []


def test_snaptrade_account_is_not_swept_for_work_on_another_broker(db, monkeypatch):
    """A ladder order resting on the subscriber's Webull account is the Webull
    sweep's job; it must not spend SnapTrade's shared quota."""
    _account(db, _SUB, BrokerName.SNAPTRADE)
    wb = _account(db, _SUB, BrokerName.WEBULL)
    _ladder_order(db, wb, _SUB, boid="WB-TP")
    assert _snap_swept(monkeypatch) == []


def test_snaptrade_trader_is_never_swept(db, monkeypatch):
    """Traders' SnapTrade listener reads their orders already."""
    acct = _account(db, _TRADER, BrokerName.SNAPTRADE)
    _ladder_order(db, acct, _TRADER, boid="ST-T")
    assert _snap_swept(monkeypatch) == []


# ── SnapTrade: the sweep turns a filled take-profit into a filled row ───────

def _snap_feed_order(boid, status="EXECUTED"):
    return {
        "brokerage_order_id": boid, "status": status,
        "filled_units": "3", "execution_price": "1.42",
        "time_placed": "2026-10-13T13:45:00Z",
        "time_executed": _FILLED_AT.isoformat(),
    }


class _CountingSnapTrade(SnapTradeAdapter):
    def __init__(self, feed):
        super().__init__({})
        self._feed = feed
        self.list_calls = 0

    def list_recent_activities(self):
        self.list_calls += 1
        return list(self._feed)

    def get_positions(self):
        return []


def test_snaptrade_take_profit_that_filled_is_recorded_filled(db):
    acct = _account(db, _SUB, BrokerName.SNAPTRADE)
    tp = _ladder_order(db, acct, _SUB, boid="ST-TP")
    adapter = _CountingSnapTrade([_snap_feed_order("ST-TP")])

    snap.reconcile_subscriber_account(_SUB, acct, adapter=adapter)
    db.refresh(tp)

    assert tp.status == OrderStatus.FILLED
    assert tp.filled_quantity == Decimal("3")
    assert tp.filled_avg_price == Decimal("1.42")
    assert tp.broker_filled_at.replace(tzinfo=timezone.utc) == _FILLED_AT
    assert tp.closed_at is not None


def test_snaptrade_snapshot_reads_several_working_orders_in_one_call(db):
    """A take-profit and its stop used to cost one full order-history read
    EACH; the snapshot makes it one for the refresh, whatever the count."""
    import app.services.fills_sync as fs

    acct_id = _account(db, _SUB, BrokerName.SNAPTRADE)
    tp = _ladder_order(db, acct_id, _SUB, boid="ST-TP")
    sl = _ladder_order(db, acct_id, _SUB, boid="ST-SL", order_type=OrderType.STOP)
    adapter = _CountingSnapTrade([
        _snap_feed_order("ST-TP"), _snap_feed_order("ST-SL", status="CANCELED"),
    ])

    fs._refresh_open_orders(db, db.get(BrokerAccount, acct_id), adapter)
    db.commit()
    db.refresh(tp)
    db.refresh(sl)

    assert adapter.list_calls == 1
    assert tp.status == OrderStatus.FILLED
    assert sl.status == OrderStatus.CANCELED


def test_snaptrade_snapshot_matches_get_order():
    adapter = _CountingSnapTrade([_snap_feed_order("A"), _snap_feed_order("B", "ACCEPTED")])
    snap_map = adapter.get_orders_snapshot()
    assert set(snap_map) == {"A", "B"}
    for boid in ("A", "B"):
        one = adapter.get_order(boid)
        assert (snap_map[boid].status, snap_map[boid].filled_avg_price) == \
            (one.status, one.filled_avg_price)


# ── direct Webull: the existing subscriber sweep already covers the ladder ──

class _WebullStub:
    def __init__(self, results):
        self._results = results
        self.calls: list[str] = []

    def get_orders_snapshot(self):
        self.calls.append("snapshot")
        return dict(self._results)

    def get_order(self, boid):
        self.calls.append(boid)
        return self._results[boid]


def _webull_filled(boid):
    return BrokerOrderResult(
        broker_order_id=boid, status=OrderStatus.FILLED, submitted_at=_FILLED_AT,
        filled_quantity=Decimal("3"), filled_avg_price=Decimal("1.42"),
        filled_at=_FILLED_AT,
    )


def test_webull_take_profit_that_filled_is_recorded_filled(db, monkeypatch):
    acct = _account(db, _SUB, BrokerName.WEBULL)
    tp = _ladder_order(db, acct, _SUB, boid="tp-coid")
    stub = _WebullStub({"tp-coid": _webull_filled("tp-coid")})
    monkeypatch.setattr(webull_rec, "adapter_for", lambda a, c: stub)
    monkeypatch.setattr(webull_rec, "decrypt_json", lambda blob: {})

    webull_rec._reconcile_once()
    db.refresh(tp)

    assert stub.calls == ["tp-coid"]          # one order, one read
    assert tp.status == OrderStatus.FILLED
    assert tp.filled_avg_price == Decimal("1.42")
    assert tp.broker_filled_at.replace(tzinfo=timezone.utc) == _FILLED_AT
    assert tp.closed_at is not None


def test_webull_idle_subscriber_makes_no_broker_calls(db, monkeypatch):
    acct = _account(db, _SUB, BrokerName.WEBULL)
    _ladder_order(db, acct, _SUB, boid="done", status=OrderStatus.FILLED)

    def _no_adapter(*_a, **_k):
        raise AssertionError("an idle account must not build an adapter")

    monkeypatch.setattr(webull_rec, "adapter_for", _no_adapter)
    webull_rec._reconcile_once()
