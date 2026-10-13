"""A sell the app confirmed filled keeps its price and time.

QA 2026-10-13, SPY 776P: the On Fill stop sold 4 at market, exit_confirm saw it
fill and marked it FILLED — with no price and no close time. Closed today picks a
day's sells by fill/close time, so the sell vanished from it, and realized P&L
had no price to work with. A Discord subscriber's Webull account has no order
listener, so nothing came back to fill them in.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType, OptionRight
from app.models.user import User, UserRole
from app.services import exit_confirm, fills_sync

FILLED_AT = datetime(2026, 10, 13, 13, 43, 32, tzinfo=timezone.utc)


def _result(status=OrderStatus.FILLED, px=Decimal("0.29"), at=FILLED_AT):
    return SimpleNamespace(status=status, filled_quantity=Decimal(4),
                           filled_avg_price=px, filled_at=at)


class _Adapter:
    def __init__(self, result=None, raises=False):
        self.result, self.raises, self.reads = result, raises, 0

    def get_order(self, _id):
        self.reads += 1
        if self.raises:
            raise RuntimeError("rate limited")
        return self.result


# ── exit_confirm records what the broker said ────────────────────────────────

def _sell(**kw):
    base = dict(id=uuid.uuid4(), status=OrderStatus.SUBMITTED, broker_order_id="b1",
                filled_quantity=Decimal(0), filled_avg_price=None,
                broker_filled_at=None, closed_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_confirmed_sell_gets_its_price_and_times(monkeypatch):
    order = _sell()
    adapter = _Adapter(_result())
    monkeypatch.setattr("app.brokers.adapter_for", lambda acct, creds: adapter)
    monkeypatch.setattr("app.services.crypto.decrypt_json", lambda v: {})
    db = SimpleNamespace(get=lambda model, key: SimpleNamespace(encrypted_credentials="x"),
                         flush=lambda: None)

    assert exit_confirm.confirm_filled(db, uuid.uuid4(), order, attempts=1, pause_s=0) is True
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price == Decimal("0.29")
    assert order.broker_filled_at == FILLED_AT
    assert order.closed_at == FILLED_AT


def test_recording_never_overwrites_what_is_known():
    order = _sell(filled_avg_price=Decimal("0.31"), broker_filled_at=FILLED_AT,
                  closed_at=FILLED_AT + timedelta(seconds=5))
    exit_confirm.record_fill_details(order, _result(px=Decimal("0.29"),
                                                    at=FILLED_AT + timedelta(minutes=1)))
    assert order.filled_avg_price == Decimal("0.31")
    assert order.broker_filled_at == FILLED_AT
    assert order.closed_at == FILLED_AT + timedelta(seconds=5)


def test_no_broker_time_still_stamps_a_close_time():
    order = _sell()
    exit_confirm.record_fill_details(order, _result(at=None))
    assert order.closed_at is not None and order.broker_filled_at is None


# ── an already-filled sell with nothing recorded is repaired ─────────────────

def _setup(db, *, age=timedelta(minutes=5), closed_at=None, price=None):
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local",
                password_hash="x", role=UserRole.SUBSCRIBER)
    db.add(user)
    acct = BrokerAccount(id=uuid.uuid4(), user_id=user.id, broker=BrokerName.FAKE,
                         label="test", encrypted_credentials="x")
    db.add(acct)
    db.flush()
    order = Order(
        id=uuid.uuid4(), user_id=user.id, broker_account_id=acct.id,
        instrument_type=InstrumentType.OPTION, symbol="SPY",
        option_strike=Decimal("776"), option_right=OptionRight.PUT,
        option_expiry=datetime(2026, 10, 13).date(),
        side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=Decimal(4),
        is_closing=True, status=OrderStatus.FILLED, filled_quantity=Decimal(4),
        filled_avg_price=price, closed_at=closed_at, broker_order_id=uuid.uuid4().hex,
        created_at=datetime.now(timezone.utc) - age,
    )
    db.add(order)
    db.flush()
    return acct, order


def test_a_filled_sell_missing_its_price_is_repaired():
    db = SessionLocal()
    try:
        acct, order = _setup(db)
        adapter = _Adapter(_result())
        assert fills_sync.repair_unrecorded_fills(db, acct, adapter) == 1
        assert order.filled_avg_price == Decimal("0.29")
        assert order.closed_at == FILLED_AT
        # Repaired once: it is not read again.
        assert fills_sync.repair_unrecorded_fills(db, acct, adapter) == 0
        assert adapter.reads == 1
    finally:
        db.rollback()


def test_a_recorded_sell_is_not_read():
    db = SessionLocal()
    try:
        acct, _ = _setup(db, closed_at=FILLED_AT, price=Decimal("0.29"))
        adapter = _Adapter(_result())
        assert fills_sync.repair_unrecorded_fills(db, acct, adapter) == 0
        assert adapter.reads == 0
    finally:
        db.rollback()


def test_a_failed_read_is_retried_while_recent():
    db = SessionLocal()
    try:
        acct, order = _setup(db)
        assert fills_sync.repair_unrecorded_fills(db, acct, _Adapter(raises=True)) == 0
        assert order.closed_at is None
    finally:
        db.rollback()


def test_an_order_the_broker_never_reports_is_given_up_after_an_hour():
    db = SessionLocal()
    try:
        acct, order = _setup(db, age=timedelta(hours=2))
        fills_sync.repair_unrecorded_fills(db, acct, _Adapter(raises=True))
        assert order.closed_at is not None
    finally:
        db.rollback()


def test_old_orders_are_outside_the_window():
    db = SessionLocal()
    try:
        acct, _ = _setup(db, age=timedelta(days=2))
        adapter = _Adapter(_result())
        assert fills_sync.repair_unrecorded_fills(db, acct, adapter) == 0
        assert adapter.reads == 0
    finally:
        db.rollback()
