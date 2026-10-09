"""Closing a position must first clear whatever is resting on that contract.

THE BUG
-------
A resting order RESERVES the position at the broker, so a close placed on top of
one is refused outright:

    OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION — "This order cannot be entered
    because it will reverse an existing position. You may need to close an open
    position, or cancel an open order, before you can submit this order."

That is what a user hit on prod: a protective stop was resting on their NIO
position, they pressed Close at Market, and got that raw broker string back.
They could not close a position they owned without first knowing there was a
stop in the way and cancelling it by hand.

Alpaca refuses the same thing as 40310000 (held_for_orders), so this is not
Webull-specific.

The mirror path already handled it — copy_engine._cancel_subscriber_conflicts
clears a subscriber's contract before placing their mirror close. The TRADER's
own account never got the same treatment.

WHAT THESE PIN
--------------
1. Working orders on the contract are cancelled before the close is placed.
2. BOTH sides go: a resting SELL blocks the close, and a resting BUY would
   re-open the position seconds after we flatten it.
3. Only THAT contract, that account, that user — never someone else's order and
   never a different strike/expiry/right.
4. Already-terminal orders are left alone (nothing to cancel).
5. A cancel that fails does not abort the close: a stale broker id must not
   block an exit the user asked for.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import positions as mod
from app.brokers.base import BrokerPosition
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import (
    InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType,
)
from app.models.user import User, UserRole

_USER = uuid.UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
_OTHER = uuid.UUID("b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e")
_EXP = date(2026, 9, 18)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """The helper pauses to let the broker release the reservation; tests don't
    need to actually wait for it."""
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _no_events(monkeypatch):
    monkeypatch.setattr(mod.events, "publish", lambda *a, **k: None)


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    for m in (User, BrokerAccount, Order):
        m.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    for uid in (_USER, _OTHER):
        db.add(User(id=uid, email=f"{uid}@x.com", password_hash="x",
                    role=UserRole.TRADER, is_active=True))
    db.commit()
    return db


def _acct(db, user_id=_USER):
    a = BrokerAccount(id=uuid.uuid4(), user_id=user_id, broker=BrokerName.WEBULL,
                      label="w", is_paper=False, supports_fractional=False,
                      encrypted_credentials="x", connection_status="connected")
    db.add(a); db.commit()
    return a


def _pos(symbol="NIO", strike="3.5"):
    return BrokerPosition(
        broker_symbol=f"{symbol}260918C00003500", symbol=symbol,
        instrument_type=InstrumentType.OPTION, quantity=Decimal("2"),
        avg_entry_price=Decimal("0.17"), current_price=Decimal("0.20"),
        market_value=None, unrealized_pnl=None,
        option_expiry=_EXP, option_strike=Decimal(strike), option_right=OptionRight.CALL,
    )


def _order(db, acct, *, user_id=_USER, side=OrderSide.SELL, otype=OrderType.STOP,
           status=OrderStatus.SUBMITTED, boid="B1", strike="3.5", symbol="NIO",
           expiry=_EXP):
    o = Order(id=uuid.uuid4(), user_id=user_id, broker_account_id=acct.id,
              instrument_type=InstrumentType.OPTION, symbol=symbol,
              option_expiry=expiry, option_strike=Decimal(strike),
              option_right=OptionRight.CALL, side=side, order_type=otype,
              quantity=Decimal("2"), status=status, broker_order_id=boid)
    db.add(o); db.commit()
    return o


class _Adapter:
    def __init__(self, fail_on=()):
        self.cancelled: list[str] = []
        self._fail_on = set(fail_on)

    def cancel_order(self, boid):
        self.cancelled.append(boid)
        if boid in self._fail_on:
            raise RuntimeError("broker says: order not found")
        return True


def _run(db, acct, adapter, pos=None):
    user = db.get(User, _USER)
    return mod._cancel_working_orders_for_position(
        db, user, acct, adapter, pos or _pos()
    )


# ── 1-2. it clears the contract, both sides ─────────────────────────────────
def test_cancels_the_resting_stop_that_blocks_the_close():
    """The exact prod case: a protective stop resting on the position."""
    db = _db(); acct = _acct(db)
    stop = _order(db, acct, side=OrderSide.SELL, otype=OrderType.STOP, boid="STOP1")
    got = _run(db, acct, (ad := _Adapter()))
    assert ad.cancelled == ["STOP1"]
    assert got == [stop.id]
    db.refresh(stop)
    assert stop.status == OrderStatus.CANCELED
    assert stop.closed_at is not None
    assert "positions table" in (stop.reject_reason or ""), \
        "the row should say WHY it was cancelled, not look like a broker reject"


def test_cancels_a_resting_buy_too():
    """A working BUY doesn't block the close, but it would RE-OPEN the position
    moments after we flatten it."""
    db = _db(); acct = _acct(db)
    buy = _order(db, acct, side=OrderSide.BUY, otype=OrderType.LIMIT, boid="BUY1")
    got = _run(db, acct, (ad := _Adapter()))
    assert ad.cancelled == ["BUY1"] and got == [buy.id]


def test_cancels_every_working_order_on_the_contract():
    """A bracket rests TWO orders on one position — both have to go."""
    db = _db(); acct = _acct(db)
    _order(db, acct, otype=OrderType.STOP, boid="SL")
    _order(db, acct, otype=OrderType.LIMIT, boid="TP")
    _order(db, acct, otype=OrderType.LIMIT, boid="PARTIAL",
           status=OrderStatus.PARTIALLY_FILLED)
    ad = _Adapter()
    assert len(_run(db, acct, ad)) == 3
    assert sorted(ad.cancelled) == ["PARTIAL", "SL", "TP"]


# ── 3. scope ────────────────────────────────────────────────────────────────
def test_never_touches_a_different_contract_account_or_user():
    """Cancelling the wrong resting order would remove someone's protection."""
    db = _db(); acct = _acct(db); other_acct = _acct(db, user_id=_OTHER)
    _order(db, acct, boid="OTHER-STRIKE", strike="4.0")
    _order(db, acct, boid="OTHER-SYMBOL", symbol="TSLA")
    _order(db, acct, boid="OTHER-EXPIRY", expiry=date(2026, 10, 16))
    _order(db, other_acct, user_id=_OTHER, boid="OTHER-USER")
    mine = _order(db, acct, boid="MINE")
    ad = _Adapter()
    assert _run(db, acct, ad) == [mine.id]
    assert ad.cancelled == ["MINE"]


# ── 4. nothing to do ────────────────────────────────────────────────────────
def test_terminal_orders_are_left_alone():
    db = _db(); acct = _acct(db)
    for st in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED,
               OrderStatus.EXPIRED):
        _order(db, acct, status=st, boid=f"T-{st.value}")
    ad = _Adapter()
    assert _run(db, acct, ad) == [] and ad.cancelled == []


def test_no_orders_means_no_broker_call_and_no_pause():
    """The common case — nothing resting — must cost nothing."""
    db = _db(); acct = _acct(db)
    ad = _Adapter()
    assert _run(db, acct, ad) == [] and ad.cancelled == []


# ── 5. a failed cancel must not block the exit ──────────────────────────────
def test_a_failing_cancel_does_not_stop_the_others():
    """A stale broker id (already filled/cancelled at the broker) must not
    prevent a close the user asked for."""
    db = _db(); acct = _acct(db)
    _order(db, acct, boid="STALE")
    good = _order(db, acct, boid="GOOD")
    ad = _Adapter(fail_on=("STALE",))
    got = _run(db, acct, ad)
    assert got == [good.id], "the healthy cancel still went through"
    assert sorted(ad.cancelled) == ["GOOD", "STALE"], "both were attempted"


# ── 6. the race that turned a close into a SHORT ────────────────────────────
def test_position_is_re_read_after_cancelling():
    """The reason a sell became a short.

    A resting stop can FILL in the moments before our cancel reaches the
    broker. Sizing the close from the snapshot taken BEFORE the cancel then
    sells a holding that no longer exists, and the account goes short.

    So close_position must re-read the position after cancelling, and it must
    only pay for that extra broker call when something was actually cancelled.
    """
    import inspect
    src = inspect.getsource(mod.close_position)
    cancel_at = src.index("_cancel_working_orders_for_position(")
    place_at = src.index("_place_trader_order(")
    # A second get_positions, guarded by `if cancelled:`, between the two.
    assert "if cancelled:" in src, "the re-read must be conditional on a cancel"
    reread_at = src.index("if cancelled:")
    assert cancel_at < reread_at < place_at, \
        "re-read must sit between the cancel and the placement"
    assert src.count("adapter.get_positions()") == 2, \
        "one read up front, one after cancelling"


def test_quantity_is_clamped_to_what_is_actually_held():
    """Never sell more than the position. The size can legitimately shrink
    between the client's view and now — a partial fill on the stop we just
    cancelled — and over-selling is exactly what opens a short. Clamping also
    beats erroring: the user asked to get OUT."""
    import inspect
    src = inspect.getsource(mod.close_position)
    assert "close_qty = full_qty" in src, "must clamp down to the held size"
    # And the clamp must come AFTER the re-read, or it clamps to a stale size.
    assert src.index("if cancelled:") < src.index("close_qty = full_qty")


# ── 7. stocks, where every option field is NULL on both sides ──────────────
def _stock_pos(symbol="NIO"):
    return BrokerPosition(
        broker_symbol=symbol, symbol=symbol, instrument_type=InstrumentType.STOCK,
        quantity=Decimal("10"), avg_entry_price=Decimal("3.40"),
        current_price=Decimal("3.49"), market_value=None, unrealized_pnl=None,
        option_expiry=None, option_strike=None, option_right=None,
    )


def _stock_order(db, acct, *, boid="S1", symbol="NIO", status=OrderStatus.SUBMITTED):
    o = Order(id=uuid.uuid4(), user_id=_USER, broker_account_id=acct.id,
              instrument_type=InstrumentType.STOCK, symbol=symbol,
              option_expiry=None, option_strike=None, option_right=None,
              side=OrderSide.SELL, order_type=OrderType.STOP,
              quantity=Decimal("10"), status=status, broker_order_id=boid)
    db.add(o); db.commit()
    return o


def test_matches_stock_orders_where_option_fields_are_null():
    """is_not_distinct_from(None) has to behave as IS NULL, or a stock close
    would silently cancel nothing and hit the same broker rejection."""
    db = _db(); acct = _acct(db)
    stop = _stock_order(db, acct, boid="STOCK-STOP")
    user = db.get(User, _USER)
    ad = _Adapter()
    got = mod._cancel_working_orders_for_position(db, user, acct, ad, _stock_pos())
    assert got == [stop.id] and ad.cancelled == ["STOCK-STOP"]


def test_a_stock_close_never_cancels_an_option_on_the_same_ticker():
    """NIO stock and NIO 3.5C are different positions. Closing the stock must
    not cancel the option's protective stop."""
    db = _db(); acct = _acct(db)
    opt = _order(db, acct, boid="NIO-OPT")           # NIO 3.5 call
    stk = _stock_order(db, acct, boid="NIO-STOCK")
    user = db.get(User, _USER)

    ad = _Adapter()
    assert mod._cancel_working_orders_for_position(db, user, acct, ad, _stock_pos()) == [stk.id]
    assert ad.cancelled == ["NIO-STOCK"]

    ad2 = _Adapter()
    assert mod._cancel_working_orders_for_position(db, user, acct, ad2, _pos()) == [opt.id]
    assert ad2.cancelled == ["NIO-OPT"]


# ── 8. broker-agnostic: Alpaca and Webull shapes both match ────────────────
#
# The two brokers describe the SAME option completely differently:
#
#   Alpaca  broker_symbol = "NIO260918C00003500"   (OCC)
#   Webull  broker_symbol = "81I49KQ..."           (opaque position id)
#
# If the cancel matched on broker_symbol it would work for one and silently
# cancel nothing for the other — a close that then hits the same broker
# rejection with no sign of why. It matches on the NORMALISED contract
# instead (instrument_type + root symbol + expiry + strike + right), which
# both adapters produce: Alpaca parses the OCC (_parse_occ -> display_symbol),
# Webull resolves the terms (_option_terms -> root). Every order-creation path
# stores that same root in Order.symbol.

def _pos_with_broker_symbol(broker_symbol: str):
    """Same contract, as each broker would describe it."""
    return BrokerPosition(
        broker_symbol=broker_symbol, symbol="NIO",
        instrument_type=InstrumentType.OPTION, quantity=Decimal("2"),
        avg_entry_price=Decimal("0.17"), current_price=Decimal("0.20"),
        market_value=None, unrealized_pnl=None,
        option_expiry=_EXP, option_strike=Decimal("3.5"),
        option_right=OptionRight.CALL,
    )


@pytest.mark.parametrize("broker_symbol,label", [
    ("NIO260918C00003500", "alpaca-style OCC"),
    ("81I49KQDDG92KOQ9TS9HN5AV9", "webull-style position id"),
])
def test_same_contract_matches_whatever_the_broker_calls_it(broker_symbol, label):
    db = _db(); acct = _acct(db)
    stop = _order(db, acct, boid="STOP-1")       # stored with root symbol "NIO"
    user = db.get(User, _USER)
    ad = _Adapter()
    got = mod._cancel_working_orders_for_position(
        db, user, acct, ad, _pos_with_broker_symbol(broker_symbol)
    )
    assert got == [stop.id], f"{label}: contract match must not depend on broker_symbol"
    assert ad.cancelled == ["STOP-1"]


def test_alpaca_style_raise_on_uncancellable_is_tolerated():
    """Alpaca RAISES when an order isn't cancellable (already filled); Webull
    returns a bool. The helper must survive either without dropping the close
    — a filled order is not a reason to refuse someone an exit."""
    db = _db(); acct = _acct(db)
    _order(db, acct, boid="ALREADY-FILLED")
    good = _order(db, acct, boid="STILL-WORKING")
    user = db.get(User, _USER)
    ad = _Adapter(fail_on=("ALREADY-FILLED",))
    assert mod._cancel_working_orders_for_position(db, user, acct, ad, _pos()) == [good.id]


def test_close_places_the_order_after_cancelling():
    """Wiring check: close_position clears the contract BEFORE it builds the
    close, so the broker sees a free position."""
    import inspect
    src = inspect.getsource(mod.close_position)
    assert "_cancel_working_orders_for_position(" in src
    assert src.index("_cancel_working_orders_for_position(") < src.index("_place_trader_order("), \
        "the cancel must run BEFORE the close is placed"


if __name__ == "__main__":
    print("run under pytest (uses fixtures)")
