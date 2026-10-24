"""Webull's "will reverse an existing position" refusal means the contracts are
busy — never a reason to sell them.

QA 2026-10-23, TSLA 375P: a stop placed while the old take-profit pair still
held both contracts was refused with OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION.
It was read as "the stop will never rest", and a contract was sold at market to
avoid leaving it unprotected. Then, after each of two averages, the same
15-minute-old refusal sold the newly bought contract.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import discord_stop_orders as so

EXP = date(2026, 10, 23)
REVERSE = ("HTTP Status: 417, Code: OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION, Msg: This order cannot be "
           "entered because it will reverse an existing position. You may need to close an open position, "
           "or cancel an open order, before you can submit this order., RequestID: 6c1d")


def test_the_reverse_refusal_reads_as_contracts_busy():
    assert so.is_reservation_conflict(REVERSE)


def test_a_stop_refused_that_way_is_retried_not_exited(monkeypatch):
    exits = []
    g = SimpleNamespace(user_id=uuid.uuid4(), symbol="TSLA", option_strike=D("375"), option_right="put",
                        option_expiry=EXP)

    def _refuse(q, p):
        raise RuntimeError(REVERSE)

    with pytest.raises(RuntimeError):
        so._place_or_close(None, g, D(1), D("0.33"), _refuse, lambda q: exits.append(q))
    assert exits == [], "a busy-contracts refusal must never sell the position"


# ── a refusal from before the latest buy no longer counts ────────────────────

@pytest.fixture
def world():
    db = SessionLocal()
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local",
                password_hash="x", role=UserRole.TRADER)
    db.add(user)
    db.flush()
    acct = BrokerAccount(id=uuid.uuid4(), user_id=user.id, broker=BrokerName.FAKE, label="t",
                         encrypted_credentials="x")
    db.add(acct)
    db.flush()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    guard = DiscordPositionGuard(user_id=user.id, symbol="TSLA", option_strike=D("375"), option_right="put",
                                 option_expiry=EXP, sell_count=0, entry_price=D("1.69"), stop_price=D("0.33"))
    db.add(guard)
    db.flush()
    guard.created_at = t0

    def order(side, status, otype, at, reason=None):
        o = Order(id=uuid.uuid4(), user_id=user.id, broker_account_id=acct.id,
                  instrument_type=InstrumentType.OPTION, symbol="TSLA", option_strike=D("375"),
                  option_right=OptionRight.PUT, option_expiry=EXP, side=side, order_type=otype,
                  quantity=D(1), stop_price=D("0.33") if otype == OrderType.STOP else None,
                  status=status, reject_reason=reason, created_at=at)
        db.add(o)
        db.flush()
        return o

    db.commit()
    yield SimpleNamespace(db=db, guard=guard, order=order, t0=t0)
    db.rollback()
    db.query(Order).filter(Order.user_id == user.id).delete()
    db.delete(db.get(DiscordPositionGuard, guard.id))
    db.delete(db.get(BrokerAccount, acct.id))
    db.delete(db.get(User, user.id))
    db.commit()
    db.close()


def test_a_refusal_after_the_latest_buy_still_counts(world):
    world.order(OrderSide.BUY, OrderStatus.FILLED, OrderType.LIMIT, world.t0 + timedelta(seconds=5))
    world.order(OrderSide.SELL, OrderStatus.REJECTED, OrderType.STOP, world.t0 + timedelta(minutes=1),
                "price is through the stop")
    assert so._recent_rejection_reason(world.db, world.guard) == "price is through the stop"


def test_an_average_after_the_refusal_starts_the_stop_fresh(world):
    """TSLA 375P: the stop was refused at 11:27, then 1 was averaged in at 11:29
    — that refusal must not make the next pass sell the new contract."""
    world.order(OrderSide.BUY, OrderStatus.FILLED, OrderType.LIMIT, world.t0 + timedelta(seconds=5))
    world.order(OrderSide.SELL, OrderStatus.REJECTED, OrderType.STOP, world.t0 + timedelta(minutes=1),
                "price is through the stop")
    world.order(OrderSide.BUY, OrderStatus.FILLED, OrderType.LIMIT, world.t0 + timedelta(minutes=3))
    assert so._recent_rejection_reason(world.db, world.guard) is None


def test_with_the_refusal_forgotten_the_next_pass_places_a_stop_not_an_exit(world):
    world.order(OrderSide.BUY, OrderStatus.FILLED, OrderType.LIMIT, world.t0 + timedelta(seconds=5))
    world.order(OrderSide.SELL, OrderStatus.REJECTED, OrderType.STOP, world.t0 + timedelta(minutes=1),
                "price is through the stop")
    world.order(OrderSide.BUY, OrderStatus.FILLED, OrderType.LIMIT, world.t0 + timedelta(minutes=3))
    placed, exits = [], []
    import app.services.discord_stop_orders as mod

    out = mod.reconcile(world.db, world.guard, D(2),
                        place_stop=lambda q, p: placed.append((q, p)) or uuid.uuid4(),
                        cancel_stop=lambda oid: None, close_position=lambda q: exits.append(q))
    assert exits == [] and placed == [(D(2), D("0.33"))], out
