"""A trader's fill no longer sweeps a subscriber's resting ENTRY.

The entry limit keeps working at its price; the fill only syncs its status.
Closes are still forced so a subscriber is never left holding what the trader
exited.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.brokers.base import BrokerOrderResult
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import copy_engine

TRADER, SUB = uuid.uuid4(), uuid.uuid4()


def _db(*, child_is_closing: bool):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (User, BrokerAccount, Order):
        m.__table__.create(eng)
    maker = sessionmaker(bind=eng)
    db = maker()
    db.add_all([
        User(id=TRADER, email="t@x.com", password_hash="x", role=UserRole.TRADER, is_active=True, discord_enabled=False),
        User(id=SUB, email="s@x.com", password_hash="x", role=UserRole.SUBSCRIBER, is_active=True),
    ])
    acct = BrokerAccount(id=uuid.uuid4(), user_id=SUB, broker=BrokerName.IBKR, label="ibkr", is_paper=True,
                         supports_fractional=False, encrypted_credentials="x", connection_status="connected")
    db.add(acct)
    common = dict(instrument_type=InstrumentType.STOCK, symbol="TSLA", order_type=OrderType.LIMIT,
                  quantity=Decimal(2), limit_price=Decimal("375.00"))
    side = OrderSide.SELL if child_is_closing else OrderSide.BUY
    parent = Order(id=uuid.uuid4(), user_id=TRADER, side=side, status=OrderStatus.FILLED,
                   filled_quantity=Decimal(2), filled_avg_price=Decimal("374.80"), is_closing=child_is_closing, **common)
    db.add(parent); db.flush()
    child = Order(id=uuid.uuid4(), user_id=SUB, broker_account_id=acct.id, parent_order_id=parent.id,
                  side=side, status=OrderStatus.SUBMITTED, broker_order_id="1758647526",
                  is_closing=child_is_closing, **common)
    db.add(child); db.commit()
    return maker, parent.id, child.id


class _Adapter:
    """Reports the mirror as still working; records any cancel / place."""
    requires_extended_hours_limit = False

    def __init__(self):
        self.cancelled: list[str] = []
        self.placed: list = []

    def get_order(self, boid):
        return BrokerOrderResult(broker_order_id=boid, status=OrderStatus.SUBMITTED,
                                 submitted_at=datetime.now(timezone.utc), filled_quantity=Decimal(0))

    def cancel_order(self, boid):
        self.cancelled.append(boid); return True

    def place_order(self, req):
        self.placed.append(req)
        return BrokerOrderResult(broker_order_id="NEW", status=OrderStatus.FILLED,
                                 submitted_at=datetime.now(timezone.utc), filled_quantity=req.quantity,
                                 filled_avg_price=Decimal("374.79"))


def _wire(monkeypatch, maker, adapter):
    audits: list[str] = []
    # audit_logs is a JSONB table (Postgres only); capture the action names instead.
    monkeypatch.setattr(copy_engine.audit, "record", lambda db, **kw: audits.append(kw.get("action")))
    monkeypatch.setattr(copy_engine, "SessionLocal", maker)
    monkeypatch.setattr(copy_engine, "adapter_for", lambda acct, creds: adapter)
    monkeypatch.setattr(copy_engine, "decrypt_json", lambda x: {})
    monkeypatch.setattr(copy_engine.events, "publish", lambda *a, **k: None)
    return audits


def test_trader_fill_leaves_a_resting_entry_alone(monkeypatch):
    maker, parent_id, child_id = _db(child_is_closing=False)
    ad = _Adapter(); audits = _wire(monkeypatch, maker, ad)
    copy_engine.force_fill_mirrors_to_market(parent_id)
    child = maker().get(Order, child_id)
    assert child.status is OrderStatus.SUBMITTED and child.limit_price == Decimal("375.00")
    assert ad.cancelled == [] and ad.placed == []
    assert audits.count("order.mirror_left_resting") == 1


def test_trader_fill_still_forces_a_resting_close(monkeypatch):
    maker, parent_id, child_id = _db(child_is_closing=True)
    ad = _Adapter(); _wire(monkeypatch, maker, ad)
    copy_engine.force_fill_mirrors_to_market(parent_id)
    assert ad.cancelled == ["1758647526"]
    assert len(ad.placed) == 1 and ad.placed[0].is_closing is True
