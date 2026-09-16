"""Where the ladder's decision meets the order that actually goes to the broker.

The rules are unit-tested in test_discord_trim_ladder.py. What's covered here is
the wiring in _execute_signal — the places where a correct decision could still
produce a wrong order, so every assertion is against the payload handed to
placement rather than against the plan that produced it.
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
    def __init__(self, live=True):
        self.discord_quantity_multiplier = 1
        self.discord_max_per_contract = None
        self.discord_live_trading = live
        self.discord_trail_percent = Decimal("20")
        self.discord_trim_profit_gate_pct = Decimal("20")
        self.discord_trim_stop_pct = Decimal("25")
        self.discord_trim_price_threshold = Decimal("0.90")
        self.discord_trim_trail_amount = Decimal("0.25")


class _DB:
    def __init__(self, settings): self._s = settings
    def get(self, model, key): return self._s
    def add(self, obj): pass
    def flush(self): pass


class _User:
    def __init__(self): self.id = uuid.uuid4()


class _Order:
    def __init__(self): self.id = uuid.uuid4()


@pytest.fixture
def placed():
    return {}


@pytest.fixture
def harness(monkeypatch, placed):
    def _setup(*, held, rung, mark, entry="2.00", raises=None):
        user, msg = _User(), _Msg()
        db = _DB(_Settings())
        guard = DiscordPositionGuard(
            user_id=user.id, symbol="MSFT", option_strike=Decimal("100"),
            option_right=OptionRight.CALL.value, option_expiry=FUTURE,
            sell_count=rung, entry_price=Decimal(entry) if entry else None,
        )

        monkeypatch.setattr(ds.discord_execution, "resolve", lambda *a, **k: ex.Resolved(
            payload=PlaceOrderIn(
                instrument_type=InstrumentType.OPTION, symbol="MSFT",
                side=OrderSide.SELL, order_type=OrderType.MARKET,
                quantity=Decimal(held), limit_price=None, option_expiry=FUTURE,
                option_strike=Decimal("100"), option_right=OptionRight.CALL,
            ),
            broker_account_id=uuid.uuid4(), is_closing=True, resolutions={},
            mark_price=Decimal(mark) if mark else None,
            position_entry_price=Decimal(entry) if entry else None,
        ))
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
            return _Order()

        monkeypatch.setattr(trades, "_place_trader_order", _place)
        return db, user, msg, guard, retired
    return _setup


def _run(db, user, msg):
    ds._execute_signal(db, user, msg, background=None, request=None)


# ── rung 1 ───────────────────────────────────────────────────────────────────

def test_first_trim_sells_half_and_sets_the_stop(harness, placed):
    db, user, msg, guard, _ = harness(held=4, rung=0, mark="3.00")   # +50%
    _run(db, user, msg)

    assert placed["payload"].quantity == Decimal(2)
    assert placed["payload"].order_type is OrderType.MARKET
    assert guard.stop_price == Decimal("1.50")        # 25% under a 2.00 entry
    assert placed["partial_close"] is True            # subscribers must not flatten


def test_below_the_gate_places_no_order_at_all(harness, placed):
    db, user, msg, guard, _ = harness(held=4, rung=0, mark="2.10")   # +5%
    _run(db, user, msg)

    assert placed == {}
    assert guard.stop_price is None
    assert msg.status is DiscordMessageStatus.PARSED
    assert "gate" in msg.status_reason


def test_a_skipped_first_trim_still_advances_the_rung(harness, placed):
    db, user, msg, guard, _ = harness(held=4, rung=0, mark="2.10")
    _run(db, user, msg)
    assert guard.sell_count == 1


# ── rung 2 ───────────────────────────────────────────────────────────────────

def test_second_trim_on_a_cheap_contract_sells_at_market(harness, placed):
    db, user, msg, guard, _ = harness(held=2, rung=1, mark="1.00", entry="0.50")
    _run(db, user, msg)

    assert placed["payload"].quantity == Decimal(1)
    assert guard.stop_price == Decimal("0.50")        # break-even
    assert guard.trail_qty is None


def test_second_trim_on_an_expensive_contract_arms_a_trail_instead(harness, placed):
    """Nothing is sold today — the slice is parked on a trailing give-back."""
    db, user, msg, guard, _ = harness(held=2, rung=1, mark="3.00", entry="2.00")
    _run(db, user, msg)

    assert placed == {}                               # no order placed
    assert guard.trail_qty == Decimal(1)
    assert guard.trail_amount == Decimal("0.25")
    assert guard.peak_price == Decimal("3.00")        # anchored at the mark
    assert guard.stop_price == Decimal("2.00")        # break-even on the rest
    assert msg.status is DiscordMessageStatus.PARSED


# ── rung 3 ───────────────────────────────────────────────────────────────────

def test_third_trim_exits_everything_and_retires(harness, placed):
    db, user, msg, guard, retired = harness(held=3, rung=2, mark="1.00", entry="0.50")
    _run(db, user, msg)

    assert placed["payload"].quantity == Decimal(3)
    assert placed["partial_close"] is False           # a real exit — subscribers flatten
    assert retired != []


def test_a_trailing_third_trim_places_nothing_yet(harness, placed):
    db, user, msg, guard, retired = harness(held=3, rung=2, mark="4.00", entry="2.00")
    _run(db, user, msg)

    assert placed == {}
    assert guard.trail_qty == Decimal(3)
    assert retired == []                              # still owns the pending exit


# ── failure ──────────────────────────────────────────────────────────────────

def test_a_rejected_trim_gives_its_rung_back(harness):
    db, user, msg, guard, _ = harness(
        held=4, rung=0, mark="3.00", raises=RuntimeError("broker down"))
    _run(db, user, msg)

    assert guard.sell_count == 0                      # rung returned
    assert msg.status is DiscordMessageStatus.ORDER_FAILED
