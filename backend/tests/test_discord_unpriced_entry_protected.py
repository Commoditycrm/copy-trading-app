"""A Discord entry with no price still gets its On Fill stop and take-profit.

QA 2026-10-13, SPY 776P "out the gate": the alert named no price and went in at
market, so the subscriber's ladder had no entry price. Two gates then kept it
that way: the auto-trim sweep skipped guards with no entry price (so the On Fill
stop, which adopts the fill, never ran), and the poller skipped the account
because the guard had no stop yet (so nothing else adopted the fill either). The
trader, whose poller runs for other reasons, got a stop and T1; the subscriber
got nothing and rode the contract from 0.62 to 0.28.
"""
import inspect
import os
import sys
import uuid
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.pnl_poller as pp
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType, OptionRight
from app.models.user import User, UserRole
from app.services import discord_auto_trim

EXP = date(2026, 10, 13)


def _setup(db, *, status, entry_price=None):
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
        option_strike=Decimal("776"), option_right=OptionRight.PUT, option_expiry=EXP,
        side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=Decimal(4),
        is_closing=False, status=status,
        filled_quantity=Decimal(4) if status == OrderStatus.FILLED else Decimal(0),
        filled_avg_price=Decimal("0.62") if status == OrderStatus.FILLED else None,
    )
    db.add(order)
    db.flush()
    guard = DiscordPositionGuard(
        user_id=user.id, symbol="SPY", option_strike=Decimal("776"),
        option_right="put", option_expiry=EXP, sell_count=0,
        entry_price=entry_price, entry_order_id=order.id,
    )
    db.add(guard)
    db.flush()
    return user, guard


def test_the_poller_visits_a_filled_entry_with_nothing_set_yet():
    db = SessionLocal()
    try:
        user, _ = _setup(db, status=OrderStatus.FILLED)
        assert pp._has_discord_ladder_work(db, user.id) is True
    finally:
        db.rollback()


def test_an_entry_still_working_is_not_yet_poller_work():
    """Nothing to protect until it fills — and a dormant guard whose entry
    never filled must not keep an idle account polling."""
    db = SessionLocal()
    try:
        user, _ = _setup(db, status=OrderStatus.SUBMITTED)
        assert pp._has_discord_ladder_work(db, user.id) is False
    finally:
        db.rollback()


def test_a_closed_guard_is_not_poller_work():
    from datetime import datetime, timezone
    db = SessionLocal()
    try:
        user, guard = _setup(db, status=OrderStatus.FILLED)
        guard.closed_at = datetime.now(timezone.utc)
        db.flush()
        assert pp._has_discord_ladder_work(db, user.id) is False
    finally:
        db.rollback()


def test_the_sweep_includes_a_guard_with_no_entry_price():
    """The On Fill stop is what adopts the fill, so the sweep must reach it."""
    db = SessionLocal()
    try:
        user, guard = _setup(db, status=OrderStatus.FILLED, entry_price=None)
        assert guard.id in {g.id for g in discord_auto_trim._live_guards(db, user.id)}
    finally:
        db.rollback()


def test_the_on_fill_stop_adopts_the_fill_when_the_guard_had_no_price():
    """-33% from the 0.62 fill: a stop at 0.41, and the entry is now 0.62."""
    db = SessionLocal()
    try:
        _, guard = _setup(db, status=OrderStatus.FILLED, entry_price=None)

        class _Ts:
            discord_fill_stop_pct = Decimal("-33")
            discord_fill_stop_trail = False
            discord_manual_exit = False
            discord_tp_orders = True

        assert discord_auto_trim.apply_fill_stop(db, guard, _Ts()) is True
        assert guard.entry_price == Decimal("0.62")
        assert guard.stop_price == Decimal("0.41")
    finally:
        db.rollback()


def test_rungs_still_wait_for_a_price():
    """Only the On Fill step runs on an unpriced guard; the rung logic, which
    measures from the entry, filters it out."""
    src = inspect.getsource(discord_auto_trim._sweep_trader)
    fill = src.index("apply_fill_stop(")
    gate = src.index("g.entry_price is not None")
    assert fill < gate


def test_the_trader_gate_uses_the_same_selection():
    src = inspect.getsource(pp._account_role)
    assert src.count("_has_discord_ladder_work(db, user_id)") == 2
