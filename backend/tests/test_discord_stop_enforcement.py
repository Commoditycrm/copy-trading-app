"""What the poller does with the stops and trailing exits a trim left behind.

Both are emulated, so this module IS the stop — there is nothing resting at the
broker. These tests pin the two failure directions that matter: never exiting
something that shouldn't exit, and never forgetting an exit that failed.
"""
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_position_guard as guards
import app.services.discord_trailing_stop as stops
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight

EXP = date(2026, 10, 16)


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    DiscordPositionGuard.__table__.create(eng)
    with Session(eng) as s:
        yield s


class _Pos:
    def __init__(self, price, qty=4):
        self.symbol = "MSFT"
        self.option_strike = Decimal("100")
        self.option_right = OptionRight.CALL
        self.option_expiry = EXP
        self.quantity = Decimal(qty)
        self.current_price = Decimal(price)
        self.instrument_type = InstrumentType.OPTION


class _Adapter:
    def __init__(self, positions, raises=False):
        self._p, self._raises = positions, raises
    def get_positions(self):
        if self._raises:
            raise RuntimeError("broker unreachable")
        return self._p


@pytest.fixture
def sold():
    return []


@pytest.fixture
def closer(sold):
    def _close(pos, guard, quantity):
        sold.append(Decimal(str(quantity)))
    return _close


def _guard(db, *, stop=None, trail_qty=None, trail_amount=None, peak=None, user=None):
    g = DiscordPositionGuard(
        user_id=user or uuid.uuid4(), symbol="MSFT", option_strike=Decimal("100"),
        option_right=OptionRight.CALL.value, option_expiry=EXP,
        sell_count=1, entry_price=Decimal("2.00"),
        stop_price=stop, trail_qty=trail_qty, trail_amount=trail_amount,
        peak_price=peak,
    )
    db.add(g); db.flush()
    return g


# ── the hard stop ────────────────────────────────────────────────────────────

def test_a_break_below_the_stop_closes_everything(db, closer, sold):
    g = _guard(db, stop=Decimal("1.50"))
    n = stops.enforce(db, g.user_id, _Adapter([_Pos("1.49", qty=4)]), closer)

    assert n == 1
    assert sold == [Decimal(4)]              # the whole position, not a slice
    assert g.closed_at is not None


def test_sitting_exactly_on_the_stop_closes(db, closer, sold):
    g = _guard(db, stop=Decimal("1.50"))
    stops.enforce(db, g.user_id, _Adapter([_Pos("1.50")]), closer)
    assert sold == [Decimal(4)]


def test_above_the_stop_does_nothing(db, closer, sold):
    g = _guard(db, stop=Decimal("1.50"))
    assert stops.enforce(db, g.user_id, _Adapter([_Pos("1.51")]), closer) == 0
    assert sold == []
    assert g.closed_at is None


# ── the trailing slice ───────────────────────────────────────────────────────

def test_a_rising_price_raises_the_peak_without_exiting(db, closer, sold):
    g = _guard(db, trail_qty=Decimal(2), trail_amount=Decimal("0.25"), peak=Decimal("3.00"))
    stops.enforce(db, g.user_id, _Adapter([_Pos("3.50")]), closer)

    assert g.peak_price == Decimal("3.50")
    assert sold == []


def test_a_give_back_inside_the_trail_holds(db, closer, sold):
    g = _guard(db, trail_qty=Decimal(2), trail_amount=Decimal("0.25"), peak=Decimal("3.00"))
    stops.enforce(db, g.user_id, _Adapter([_Pos("2.80")]), closer)   # gave back 0.20
    assert sold == []


def test_a_give_back_past_the_trail_sells_only_the_slice(db, closer, sold):
    g = _guard(db, trail_qty=Decimal(2), trail_amount=Decimal("0.25"), peak=Decimal("3.00"))
    stops.enforce(db, g.user_id, _Adapter([_Pos("2.74", qty=4)]), closer)

    assert sold == [Decimal(2)]              # the slice, not the position
    assert g.trail_qty is None               # trail consumed
    assert g.closed_at is None               # remainder still held and tracked


def test_a_trailing_exit_taking_everything_retires(db, closer, sold):
    g = _guard(db, trail_qty=Decimal(4), trail_amount=Decimal("0.25"), peak=Decimal("3.00"))
    stops.enforce(db, g.user_id, _Adapter([_Pos("2.70", qty=4)]), closer)

    assert sold == [Decimal(4)]
    assert g.closed_at is not None


def test_the_slice_never_exceeds_what_is_held(db, closer, sold):
    """The position shrank behind our back — sell what's there, not what we
    earmarked, or the broker rejects it or we go short."""
    g = _guard(db, trail_qty=Decimal(4), trail_amount=Decimal("0.25"), peak=Decimal("3.00"))
    stops.enforce(db, g.user_id, _Adapter([_Pos("2.70", qty=2)]), closer)
    assert sold == [Decimal(2)]


# ── the stop wins over the trail ─────────────────────────────────────────────

def test_the_stop_takes_priority_over_a_pending_trail(db, closer, sold):
    """Below the floor there's no sense letting a slice keep riding — the trader
    wanted out under that price."""
    g = _guard(db, stop=Decimal("1.50"),
               trail_qty=Decimal(2), trail_amount=Decimal("0.25"), peak=Decimal("3.00"))
    stops.enforce(db, g.user_id, _Adapter([_Pos("1.40", qty=4)]), closer)

    assert sold == [Decimal(4)]              # everything, not the 2-slice
    assert g.closed_at is not None


# ── failure directions ───────────────────────────────────────────────────────

def test_a_broker_read_failure_exits_nothing(db, closer, sold):
    g = _guard(db, stop=Decimal("1.50"))
    assert stops.enforce(db, g.user_id, _Adapter([], raises=True), closer) == 0
    assert sold == []
    assert g.closed_at is None


def test_a_missing_mark_exits_nothing(db, closer, sold):
    g = _guard(db, stop=Decimal("1.50"))
    p = _Pos("1.00"); p.current_price = None
    stops.enforce(db, g.user_id, _Adapter([p]), closer)
    assert sold == []


def test_a_vanished_position_retires_its_guard(db, closer, sold):
    g = _guard(db, stop=Decimal("1.50"))
    stops.enforce(db, g.user_id, _Adapter([]), closer)

    assert g.closed_at is not None
    assert sold == []


def test_a_failed_stop_out_stays_armed_for_the_next_tick(db):
    def _boom(pos, guard, quantity):
        raise RuntimeError("broker rejected")

    g = _guard(db, stop=Decimal("1.50"))
    stops.enforce(db, g.user_id, _Adapter([_Pos("1.40")]), _boom)

    assert g.closed_at is None               # not retired
    assert g.stop_price == Decimal("1.50")   # still protecting


def test_a_failed_trailing_exit_keeps_its_slice(db):
    def _boom(pos, guard, quantity):
        raise RuntimeError("broker rejected")

    g = _guard(db, trail_qty=Decimal(2), trail_amount=Decimal("0.25"), peak=Decimal("3.00"))
    stops.enforce(db, g.user_id, _Adapter([_Pos("2.70")]), _boom)

    assert g.trail_qty == Decimal(2)         # not consumed


def test_guards_with_nothing_set_are_not_enforced(db, closer, sold):
    """A position between rungs has no stop and no trail — it must not be picked
    up, or every open position would cost a broker read every tick."""
    g = _guard(db)
    assert stops.enforce(db, g.user_id, _Adapter([_Pos("0.01")]), closer) == 0
    assert sold == []


def test_only_this_traders_guards_are_touched(db, closer, sold):
    mine = _guard(db, stop=Decimal("1.50"))
    _guard(db, stop=Decimal("9.99"), user=uuid.uuid4())

    stops.enforce(db, mine.user_id, _Adapter([_Pos("1.40")]), closer)
    assert sold == [Decimal(4)]
