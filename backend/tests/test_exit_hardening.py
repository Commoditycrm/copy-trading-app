"""Exits that behave the same in the market as they do on paper.

Each test here pins one failure seen live on 2026-09-29 (Alpaca paper, SPY
puts), so it cannot quietly come back:

  * a refused stop's exit retired the ladder before it filled; the exits sent
    after 16:00 expired, leaving positions held with nothing managing them
  * a trailing exit that took everything cleared its trail, so the guard was
    neither armed nor retired
  * a stop that rounded to $0.00 was sent, failed validation, and the "refusal"
    sold the whole position
  * a manual close was refused as OPENING a short because the ladder's resting
    stop still reserved the contracts
  * a Simulated Prices pin moved the ladder while Alpaca judged the REAL price,
    so the break-even stop it set was refused and the rest was sold
"""
import inspect
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_position_guard as guards
import app.services.discord_stop_orders as so
import app.services.discord_trailing_stop as stops
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight
from app.services import market_hours

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
    def __init__(self, positions):
        self._p = positions

    def get_positions(self):
        return self._p


def _guard(db, **kw):
    g = DiscordPositionGuard(
        user_id=uuid.uuid4(), symbol="MSFT", option_strike=Decimal("100"),
        option_right=OptionRight.CALL.value, option_expiry=EXP,
        sell_count=1, entry_price=Decimal("2.00"), **kw,
    )
    db.add(g); db.flush()
    return g


@pytest.fixture
def market(monkeypatch):
    """Controls the session clock and the order book the rules consult."""
    state = SimpleNamespace(open=True, working=None)
    monkeypatch.setattr(market_hours, "in_regular_session", lambda *a, **k: state.open)
    monkeypatch.setattr(so, "working_exit", lambda db, g: state.working)
    monkeypatch.setattr(so, "_recent_rejection_reason", lambda db, g: None)
    return state


# ── an exit in flight is never doubled ──────────────────────────────────────

def test_a_working_exit_blocks_a_second_stop_out(db, market):
    g = _guard(db, stop_price=Decimal("1.50"))
    sold = []
    market.working = object()
    stops.enforce(db, g.user_id, _Adapter([_Pos("1.40")]), lambda p, gg, q: sold.append(q))
    assert sold == []


def test_the_stop_reconciler_waits_while_an_exit_works(db, market):
    """Placing a stop now would reserve contracts the exit is selling; the
    refusal would then trigger ANOTHER exit."""
    g = _guard(db, stop_price=Decimal("1.50"))
    market.working = object()
    placed = []
    out = so.reconcile(db, g, Decimal(4), lambda q, p: placed.append((q, p)), lambda oid: None,
                       close_position=lambda q: None)
    assert out == "waiting (exit working)"
    assert placed == []


# ── options exits wait for the session ──────────────────────────────────────

def test_an_option_stop_out_waits_for_the_regular_session(db, market):
    g = _guard(db, stop_price=Decimal("1.50"))
    sold = []
    market.open = False
    stops.enforce(db, g.user_id, _Adapter([_Pos("1.40")]), lambda p, gg, q: sold.append(q))
    assert sold == [] and g.closed_at is None
    market.open = True
    stops.enforce(db, g.user_id, _Adapter([_Pos("1.40")]), lambda p, gg, q: sold.append(q))
    assert sold == [Decimal(4)]


def test_a_refused_stops_exit_is_deferred_after_the_close(db, market):
    g = _guard(db, stop_price=Decimal("1.50"))
    market.open = False
    closed = []

    def refuse(q, p):
        raise RuntimeError('{"code":42210000,"message":"stop price must be less than current price"}')

    so.reconcile(db, g, Decimal(4), refuse, lambda oid: None, close_position=closed.append)
    assert closed == []
    assert g.closed_at is None       # still managed, so the open retries it


# ── a full trailing exit stays managed until flat ───────────────────────────

def test_a_trail_that_takes_everything_stays_armed_until_flat(db, market):
    g = _guard(db, trail_qty=Decimal(4), trail_amount=Decimal("0.25"), peak_price=Decimal("3.00"))
    sold = []
    stops.enforce(db, g.user_id, _Adapter([_Pos("2.70")]), lambda p, gg, q: sold.append(q))
    assert sold == [Decimal(4)]
    assert g.trail_qty == Decimal(4)          # still armed...
    assert g in guards.armed(db)
    stops.enforce(db, g.user_id, _Adapter([]), lambda p, gg, q: sold.append(q))
    assert g.closed_at is not None            # ...so the flat tick retires it


def test_a_partial_trail_hands_the_rest_to_the_stop(db, market):
    g = _guard(db, stop_price=Decimal("1.50"), trail_qty=Decimal(2),
               trail_amount=Decimal("0.25"), peak_price=Decimal("3.00"))
    stops.enforce(db, g.user_id, _Adapter([_Pos("2.70")]), lambda p, gg, q: None)
    assert g.trail_qty is None and g.closed_at is None


# ── a $0 stop is no stop ────────────────────────────────────────────────────

def test_a_stop_that_rounds_to_zero_is_dropped():
    """Entry 0.02 at -90% is 0.002 -> 0.00."""
    assert guards._armable_stop(Decimal("0.002"), Decimal("0.02")) is None


def test_the_reconciler_never_places_a_zero_stop(db, market):
    g = _guard(db, stop_price=Decimal("0"))
    placed = []
    out = so.reconcile(db, g, Decimal(4), lambda q, p: placed.append(p), lambda oid: None)
    assert out == "idle" and placed == []


# ── manual closes free the ladder's stop first ──────────────────────────────

def test_a_manual_close_releases_the_ladder_stop(db, monkeypatch):
    g = _guard(db, stop_price=Decimal("1.50"))
    g.stop_order_id = uuid.uuid4()
    cancelled = []
    import app.api.discord_sources as ds
    monkeypatch.setattr(ds, "_cancel_stop_order", lambda db, user: cancelled.append)

    assert so.release_for_position(db, SimpleNamespace(id=g.user_id), _Pos("2.00"))
    assert len(cancelled) == 1 and g.stop_order_id is None


def test_both_manual_close_paths_release_it():
    from app.api import positions

    for fn in (positions.close_position, positions.close_all_positions):
        assert "release_for_position(db, user, pos)" in inspect.getsource(fn), fn.__name__


# ── pins never reach the broker as stops ────────────────────────────────────

def test_a_pinned_contract_rests_no_stop_at_the_broker():
    from app.services import pnl_poller

    src = inspect.getsource(pnl_poller._enforce_discord_trailing_stops)
    pinned = src[src.index("_pinned(acct.user_id, guard)"):]
    assert pinned.index("release(") < pinned.index("continue") < pinned.index("reconcile(")


# ── a full exit that never reached the broker reopens its ladder ────────────

def test_a_failed_full_exit_reopens_the_guard(db):
    from app.api.discord_sources import _reopen

    g = _guard(db)
    g.sell_count = 3
    guards.retire(db, g, "trim 3: sold everything")
    _reopen(g)
    assert g.closed_at is None and g.closed_reason is None
    assert g.sell_count == 2


def test_a_manual_close_releases_the_ladder_stop_before_cancelling_everything():
    """The cancel-everything step commits. If the ladder's own stop were
    cancelled there while the guard still pointed at it, a reconciler tick in
    the gap would read it as removed by the trader and forget the level, so
    the part of the position the close doesn't sell would lose its stop."""
    from app.api import positions

    src = inspect.getsource(positions.close_position)
    assert src.index("release_for_position(db, user, pos)") < src.index(
        "_cancel_working_orders_for_position(db, user, acct, adapter, pos)"
    )
