"""Deferred-close hooks run after commit, not inside the sync's transaction.

QA 2026-10-21: _refresh_open_orders ran fire/cancel_deferred_closes_for_entry
inline. They open their OWN session, so the sync — holding a pool connection
and the per-user advisory lock — waited on a second connection the exhausted
pool could not give. The pool-level scenarios live in
test_pool_release_broker_io.py.
"""
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.brokers.base import BrokerOrderResult
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import copy_engine, events
from app.services import fills_sync as fs

_USER = uuid.UUID("a7b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")


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
