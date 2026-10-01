"""A Discord trader and their subscribers are independent.

Which channels the subscribers get is the only link. Nothing the trader does
with their own orders — from Discord, the Trade Panel, a close, the broker's own
app — is copied to subscribers, and no cancel, modify, fill or bulk action of
the trader's cascades onto the subscribers' orders. Traders without Discord
copy exactly as before.
"""
import asyncio
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import positions as positions_api
from app.api import trades as trades_api
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import copy_engine

TRADER, SUB = uuid.uuid4(), uuid.uuid4()


def _user(discord=True, role=UserRole.TRADER):
    return SimpleNamespace(id=TRADER, role=role, discord_enabled=discord)


def test_only_a_discord_trader_trades_independently():
    assert copy_engine.trades_independently(_user(discord=True))
    assert not copy_engine.trades_independently(_user(discord=False))
    assert not copy_engine.trades_independently(_user(role=UserRole.SUBSCRIBER))
    assert not copy_engine.trades_independently(None)


class _NoDB:
    def __getattr__(self, name):
        raise AssertionError(f"fanout touched the database ({name}) for a Discord trader")


def test_no_order_of_a_discord_trader_is_copied():
    """fanout_async is the single point every trader order flows through —
    Trade Panel, closes, Discord, and every broker listener."""
    order = SimpleNamespace(bracket_parent_id=None, status=OrderStatus.SUBMITTED, id=uuid.uuid4())
    assert asyncio.run(copy_engine.fanout_async(_NoDB(), order, _user())) == []


# ── cascades onto existing subscriber orders ────────────────────────────────

def _db(discord=True):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    for m in (User, BrokerAccount, Order):
        m.__table__.create(eng)
    maker = sessionmaker(bind=eng)
    db = maker()
    db.add_all([
        User(id=TRADER, email="t@x.com", password_hash="x", role=UserRole.TRADER,
             is_active=True, discord_enabled=discord),
        User(id=SUB, email="s@x.com", password_hash="x", role=UserRole.SUBSCRIBER, is_active=True),
    ])
    acct = BrokerAccount(id=uuid.uuid4(), user_id=SUB, broker=BrokerName.ALPACA, label="a",
                         is_paper=True, supports_fractional=True, encrypted_credentials="x",
                         connection_status="connected")
    db.add(acct)
    common = dict(instrument_type=InstrumentType.OPTION, symbol="SPY", side=OrderSide.BUY,
                  order_type=OrderType.LIMIT, quantity=Decimal(1), limit_price=Decimal("1.00"),
                  option_expiry=date(2026, 10, 2), option_strike=Decimal(765),
                  option_right=OptionRight.PUT)
    parent = Order(id=uuid.uuid4(), user_id=TRADER, status=OrderStatus.CANCELED, **common)
    db.add(parent); db.flush()
    child = Order(id=uuid.uuid4(), user_id=SUB, broker_account_id=acct.id,
                  parent_order_id=parent.id, status=OrderStatus.SUBMITTED,
                  broker_order_id="B-CHILD", **common)
    db.add(child); db.commit()
    return maker, parent.id, child.id


@pytest.fixture
def no_broker(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("a subscriber's broker was called")
    monkeypatch.setattr(trades_api, "adapter_for", _fail)
    monkeypatch.setattr(copy_engine, "adapter_for", _fail)


def test_a_discord_traders_cancel_does_not_cascade(monkeypatch, no_broker):
    maker, parent_id, child_id = _db()
    monkeypatch.setattr(trades_api, "SessionLocal", maker)
    trades_api._run_cancel_fanout_in_background(parent_id)
    assert maker().get(Order, child_id).status is OrderStatus.SUBMITTED


def test_a_discord_traders_modify_does_not_cascade(monkeypatch, no_broker):
    maker, parent_id, child_id = _db()
    monkeypatch.setattr(copy_engine, "SessionLocal", maker)
    copy_engine.propagate_modify_to_mirrors(parent_id)
    copy_engine.force_fill_mirrors_to_market(parent_id)
    copy_engine.cancel_and_replace_mirrors_for_modify(parent_id, parent_id)
    assert maker().get(Order, child_id).status is OrderStatus.SUBMITTED


def test_a_traders_cancel_without_discord_still_cascades(monkeypatch):
    """Unchanged for everyone else: the mirror is cancelled."""
    maker, parent_id, child_id = _db(discord=False)
    monkeypatch.setattr(trades_api, "SessionLocal", maker)
    cancelled = []
    monkeypatch.setattr(trades_api, "adapter_for", lambda acct, creds: SimpleNamespace(
        cancel_order=lambda boid: cancelled.append(boid)))
    monkeypatch.setattr(trades_api, "decrypt_json", lambda x: {})
    monkeypatch.setattr(trades_api.events, "publish", lambda *a, **k: None)
    monkeypatch.setattr(trades_api.audit, "record", lambda *a, **k: None)
    trades_api._run_cancel_fanout_in_background(parent_id)
    assert cancelled == ["B-CHILD"]


# ── the trader's "all subscribers" actions ─────────────────────────────────

def test_a_discord_trader_cannot_close_or_cancel_for_subscribers():
    req = SimpleNamespace(headers={}, client=None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(positions_api.close_all_subscribers_positions(req, None, _NoDB(), _user()))
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        asyncio.run(trades_api.cancel_all_subscribers_open_orders(req, _NoDB(), _user()))
    assert e.value.status_code == 409


def test_a_discord_traders_order_is_not_flagged_for_subscribers():
    """No fanned-out flag and no "the trader was rejected" notice to subscribers."""
    import inspect

    src = inspect.getsource(trades_api._place_trader_order)
    block = src[src.index("will_fanout = ("):src.index("trigger_fanout =")]
    assert "trades_independently(trader)" in block
