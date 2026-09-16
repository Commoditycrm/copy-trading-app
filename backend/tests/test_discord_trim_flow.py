"""Tests for the branch that turns a sell alert into a trim.

The sizing rules are unit-tested in test_discord_position_guard.py. What is
covered HERE is the wiring in _execute_signal, where those rules meet the order
that actually goes to the broker:

  * the quantity on the order is the trim, not the whole position
  * a trim keeps its guard alive, so the trail survives and the count advances
  * a trim declares itself partial, so the copy engine leaves subscribers alone
  * a trim that would take everything degrades to a plain close

Each of those is a place where a correct decision could still produce a wrong
order, which is why they are asserted against the payload handed to placement
rather than against the decision.
"""
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.api.discord_sources as ds
import app.api.trades as trades
import app.services.discord_execution as ex
import app.services.discord_position_guard as guards
from app.models.discord_message import DiscordMessageStatus
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderType
from app.schemas.order import PlaceOrderIn

FUTURE = datetime.now(timezone.utc).date() + timedelta(days=7)


class _Msg:
    def __init__(self):
        self.id = uuid.uuid4()
        self.status = DiscordMessageStatus.PARSED
        self.status_reason = None
        self.order_id = None
        self.parsed_signal = {"action": "SELL", "symbol": "MSFT"}


class _Settings:
    def __init__(self, multiplier=1, live=True):
        self.discord_quantity_multiplier = multiplier
        self.discord_max_per_contract = None
        self.discord_live_trading = live
        self.discord_trail_percent = Decimal("20")


class _DB:
    def __init__(self, settings):
        self._settings = settings
    def get(self, model, key):
        return self._settings
    def add(self, obj): pass
    def flush(self): pass


class _User:
    def __init__(self):
        self.id = uuid.uuid4()


class _Order:
    def __init__(self):
        self.id = uuid.uuid4()


def _closing_payload(qty):
    return PlaceOrderIn(
        instrument_type=InstrumentType.OPTION, symbol="MSFT",
        side=OrderSide.SELL, order_type=OrderType.MARKET,
        quantity=Decimal(qty), limit_price=None,
        option_expiry=FUTURE, option_strike=Decimal("100"),
        option_right=OptionRight.CALL,
    )


@pytest.fixture
def placed():
    """Captures what was handed to the broker, so the assertions are on the
    ORDER rather than on the decision that produced it."""
    return {}


@pytest.fixture
def harness(monkeypatch, placed):
    def _setup(*, held, multiplier, action, mark="3.00", peak=None, raises=None):
        user, msg = _User(), _Msg()
        db = _DB(_Settings(multiplier=multiplier))
        guard = DiscordPositionGuard(
            user_id=user.id, symbol="MSFT", option_strike=Decimal("100"),
            option_right=OptionRight.CALL.value, option_expiry=FUTURE,
            sell_count=2, trail_percent=Decimal("20"), peak_price=peak,
            armed_at=datetime.now(timezone.utc),
        )

        monkeypatch.setattr(ds.discord_execution, "resolve", lambda *a, **k: ex.Resolved(
            payload=_closing_payload(held),
            broker_account_id=uuid.uuid4(),
            is_closing=True,
            resolutions={},
            mark_price=Decimal(mark) if mark else None,
        ))
        monkeypatch.setattr(ds.guards, "on_sell", lambda *a, **k: guards.SellDecision(
            action=action, guard=guard, trail_percent=Decimal("20")))
        monkeypatch.setattr(ds.guards, "find", lambda *a, **k: guard)

        retired = []
        monkeypatch.setattr(ds.guards, "retire",
                            lambda db_, g, reason: retired.append(reason))
        monkeypatch.setattr(ds.events, "publish", lambda *a, **k: None)

        def _place(db_, u, payload, acct_id, bg, req, **kw):
            if raises is not None:
                raise raises
            placed["payload"] = payload
            placed["partial_close"] = kw.get("partial_close")
            placed["resolve_wash_trade"] = kw.get("resolve_wash_trade")
            return _Order()

        monkeypatch.setattr(trades, "_place_trader_order", _place)
        return db, user, msg, guard, retired
    return _setup


def _run(db, user, msg):
    ds._execute_signal(db, user, msg, background=None, request=None)


# ── the trim itself ──────────────────────────────────────────────────────────

def test_a_trim_places_the_slice_not_the_whole_position(harness, placed):
    db, user, msg, guard, _ = harness(held=6, multiplier=2, action=guards.TRIM)
    _run(db, user, msg)

    assert placed["payload"].quantity == Decimal(2)      # not 6
    assert placed["payload"].order_type is OrderType.MARKET
    assert placed["resolve_wash_trade"] is True          # still SELL_TO_CLOSE


def test_a_trim_declares_itself_partial(harness, placed):
    """The copy engine cancels a subscriber's working entry on a trader close.
    Without this flag a trim would strand them out of a trade we're still in."""
    db, user, msg, guard, _ = harness(held=6, multiplier=2, action=guards.TRIM)
    _run(db, user, msg)
    assert placed["partial_close"] is True


def test_a_trim_keeps_its_guard_alive(harness):
    """Retiring here would drop the trailing stop and restart the count, so the
    third alert would re-arm instead of closing."""
    db, user, msg, guard, retired = harness(held=6, multiplier=2, action=guards.TRIM)
    _run(db, user, msg)
    assert retired == []
    assert guard.closed_at is None


def test_a_trim_re_anchors_the_trail_upward(harness):
    db, user, msg, guard, _ = harness(
        held=6, multiplier=2, action=guards.TRIM, mark="4.00", peak=Decimal("3.00"))
    _run(db, user, msg)
    assert guard.peak_price == Decimal("4.00")


def test_a_trim_does_not_lower_the_trail(harness):
    db, user, msg, guard, _ = harness(
        held=6, multiplier=2, action=guards.TRIM, mark="9.00", peak=Decimal("10.00"))
    _run(db, user, msg)
    assert guard.peak_price == Decimal("10.00")


# ── when a trim isn't a trim ─────────────────────────────────────────────────

def test_a_trim_that_would_take_everything_becomes_a_close(harness, placed):
    db, user, msg, guard, retired = harness(held=2, multiplier=2, action=guards.TRIM)
    _run(db, user, msg)

    assert placed["payload"].quantity == Decimal(2)      # the whole position
    assert placed["partial_close"] is False             # so subscribers DO flatten
    assert retired == ["closed by exit alert"]


def test_a_real_close_sells_everything_and_retires(harness, placed):
    db, user, msg, guard, retired = harness(held=6, multiplier=2, action=guards.CLOSE)
    _run(db, user, msg)

    assert placed["payload"].quantity == Decimal(6)
    assert placed["partial_close"] is False
    assert retired == ["closed by exit alert"]


def test_arming_places_no_order_at_all(harness, placed):
    db, user, msg, guard, _ = harness(held=6, multiplier=2, action=guards.ARM_TRAIL)
    _run(db, user, msg)

    assert placed == {}
    assert msg.status is DiscordMessageStatus.PARSED
    assert "trailing stop armed" in msg.status_reason


# ── a trim whose order never placed ──────────────────────────────────────────

def test_a_rejected_trim_gives_its_step_back(harness):
    """Nothing was sold, so the next alert must still be a trim. Spending the
    step here would skip straight to closing the whole position."""
    db, user, msg, guard, _ = harness(
        held=6, multiplier=2, action=guards.TRIM, raises=RuntimeError("broker down"))
    _run(db, user, msg)

    assert guard.sell_count == 1                        # back to armed
    assert msg.status is DiscordMessageStatus.ORDER_FAILED


def test_a_rejected_close_does_not_roll_back(harness):
    """Every later alert is a close anyway, so there is no step to preserve."""
    db, user, msg, guard, _ = harness(
        held=6, multiplier=2, action=guards.CLOSE, raises=RuntimeError("broker down"))
    _run(db, user, msg)
    assert guard.sell_count == 2                        # untouched
