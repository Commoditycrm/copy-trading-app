"""Tests for the trail-then-close behaviour of Discord sell alerts.

    BUY        → open, start counting
    1st SELL   → arm a trailing stop, do NOT exit
    2nd SELL   → close

The same message means different things depending on history, so most of these
assert the SEQUENCE rather than a single call.
"""
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_position_guard as guards
import app.services.discord_trailing_stop as trail
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import OptionRight

EXP = date(2026, 10, 16)
TRAIL = Decimal("20")


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    DiscordPositionGuard.__table__.create(eng)
    with Session(eng) as s:
        yield s


def _contract(user):
    return dict(user_id=user, symbol="MSFT", strike=Decimal("100"),
                right=OptionRight.CALL, expiry=EXP)


# ── the sequence ─────────────────────────────────────────────────────────────

def test_the_first_sell_arms_a_trail_instead_of_closing(db):
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u))
    d = guards.on_sell(db, **_contract(u), trail_percent=TRAIL)

    assert d.action == guards.ARM_TRAIL
    assert d.trail_percent == TRAIL
    assert d.guard.armed_at is not None


def test_the_second_sell_closes(db):
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u))
    guards.on_sell(db, **_contract(u), trail_percent=TRAIL)
    d = guards.on_sell(db, **_contract(u), trail_percent=TRAIL)

    assert d.action == guards.CLOSE
    assert d.guard.sell_count == 2


def test_further_sells_keep_closing(db):
    """A third alert must not re-arm — the trader is still asking to be out."""
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u))
    for _ in range(2):
        guards.on_sell(db, **_contract(u), trail_percent=TRAIL)
    assert guards.on_sell(db, **_contract(u), trail_percent=TRAIL).action == guards.CLOSE


def test_a_sell_with_no_known_entry_closes_immediately(db):
    """A position opened elsewhere, or before this feature existed. Arming a
    trail on something we know nothing about would leave the trader holding what
    they asked to sell."""
    d = guards.on_sell(db, **_contract(uuid.uuid4()), trail_percent=TRAIL)
    assert d.action == guards.CLOSE


def test_adding_to_a_position_does_not_reset_the_sequence(db):
    """"Adding" increases size; it doesn't restart trail-then-exit. Resetting
    would make the next sell merely re-arm, leaving the trader in a position they
    asked twice to leave."""
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u))
    guards.on_sell(db, **_contract(u), trail_percent=TRAIL)   # armed
    guards.on_buy(db, **_contract(u))                          # an "Adding" alert

    assert guards.on_sell(db, **_contract(u), trail_percent=TRAIL).action == guards.CLOSE


def test_each_contract_is_counted_separately(db):
    """A sell on one strike must not arm or close another."""
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u))
    other = dict(_contract(u), strike=Decimal("110"))
    guards.on_buy(db, **other)

    guards.on_sell(db, **_contract(u), trail_percent=TRAIL)
    assert guards.on_sell(db, **other, trail_percent=TRAIL).action == guards.ARM_TRAIL


def test_a_retired_guard_lets_a_new_position_start_clean(db):
    u = uuid.uuid4()
    g = guards.on_buy(db, **_contract(u))
    guards.on_sell(db, **_contract(u), trail_percent=TRAIL)
    guards.retire(db, g, "closed")
    db.flush()

    guards.on_buy(db, **_contract(u))
    assert guards.on_sell(db, **_contract(u), trail_percent=TRAIL).action == guards.ARM_TRAIL


def test_the_trail_is_captured_when_armed_not_read_later(db):
    """Changing the setting afterwards must not move a stop already protecting
    a position."""
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u))
    d = guards.on_sell(db, **_contract(u), trail_percent=Decimal("15"))
    assert d.guard.trail_percent == Decimal("15")


# ── the trail itself ─────────────────────────────────────────────────────────

class _Pos:
    def __init__(self, price, qty="5"):
        self.symbol = "MSFT"
        self.option_strike = Decimal("100")
        self.option_right = OptionRight.CALL
        self.option_expiry = EXP
        self.quantity = Decimal(qty)
        self.current_price = Decimal(str(price))
        self.market_value = None


class _Adapter:
    def __init__(self, positions, raises=False):
        self._p = positions
        self._raises = raises

    def get_positions(self):
        if self._raises:
            raise RuntimeError("broker down")
        return self._p


def _armed(db, user, peak=None):
    guards.on_buy(db, **_contract(user))
    guards.on_sell(db, **_contract(user), trail_percent=TRAIL)
    g = guards.find(db, **_contract(user))
    g.peak_price = Decimal(str(peak)) if peak is not None else None
    db.flush()
    return g


def test_a_rising_price_raises_the_peak_and_does_not_close(db):
    u = uuid.uuid4()
    _armed(db, u, peak=10)
    closed = []
    n = trail.enforce(db, u, _Adapter([_Pos(12)]), lambda p, g: closed.append(g))

    assert n == 0 and not closed
    assert guards.find(db, **_contract(u)).peak_price == Decimal("12")


def test_a_retrace_past_the_trail_closes(db):
    """Peak 10, 20% trail → exit at or below 8."""
    u = uuid.uuid4()
    _armed(db, u, peak=10)
    closed = []
    n = trail.enforce(db, u, _Adapter([_Pos("7.90")]), lambda p, g: closed.append(g))

    assert n == 1 and len(closed) == 1
    assert guards.find(db, **_contract(u)) is None      # retired


def test_a_retrace_inside_the_trail_holds(db):
    u = uuid.uuid4()
    _armed(db, u, peak=10)
    closed = []
    trail.enforce(db, u, _Adapter([_Pos("8.50")]), lambda p, g: closed.append(g))
    assert not closed


def test_a_broker_read_failure_closes_nothing(db):
    """A failed read is not a reason to exit a position."""
    u = uuid.uuid4()
    _armed(db, u, peak=10)
    closed = []
    assert trail.enforce(db, u, _Adapter([], raises=True), lambda p, g: closed.append(g)) == 0
    assert not closed


def test_a_position_that_vanished_retires_its_guard(db):
    """Closed by hand, expired, or stopped out elsewhere."""
    u = uuid.uuid4()
    _armed(db, u, peak=10)
    trail.enforce(db, u, _Adapter([]), lambda p, g: None)
    assert guards.find(db, **_contract(u)) is None


def test_a_failed_close_leaves_the_guard_armed(db):
    """An exit that failed once must be retried, not forgotten."""
    u = uuid.uuid4()
    _armed(db, u, peak=10)

    def _boom(p, g):
        raise RuntimeError("broker rejected")

    assert trail.enforce(db, u, _Adapter([_Pos("7.00")]), _boom) == 0
    assert guards.find(db, **_contract(u)) is not None


def test_the_peak_survives_a_restart(db):
    """The peak lives on the row, so a restart resumes the trail where it was
    rather than re-anchoring to the current price — which would silently widen
    the stop."""
    u = uuid.uuid4()
    _armed(db, u, peak=10)
    trail.enforce(db, u, _Adapter([_Pos("9.00")]), lambda p, g: None)
    assert guards.find(db, **_contract(u)).peak_price == Decimal("10")


def test_an_unarmed_position_is_not_trailed(db):
    """A position that has taken no sell alert has no stop to enforce."""
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u))
    closed = []
    assert trail.enforce(db, u, _Adapter([_Pos("1.00")]), lambda p, g: closed.append(g)) == 0
