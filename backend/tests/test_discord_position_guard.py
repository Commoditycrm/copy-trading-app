"""Guard bookkeeping: opening, finding and retiring the row the ladder runs on.

The ladder's RULES live in test_discord_trim_ladder.py and its enforcement in
test_discord_stop_enforcement.py. What's covered here is the row itself — that
one position gets one guard, that contracts don't bleed into each other, and
that adding to a position never disturbs a ladder already in progress.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_position_guard as guards
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import OptionRight

EXP = date(2026, 10, 16)
CFG = guards.TrimConfig()


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    DiscordPositionGuard.__table__.create(eng)
    with Session(eng) as s:
        yield s


def _contract(user, strike="100", right=OptionRight.CALL):
    return dict(user_id=user, symbol="MSFT", strike=Decimal(strike),
                right=right, expiry=EXP)


# ── opening ──────────────────────────────────────────────────────────────────

def test_a_buy_opens_one_guard_and_remembers_the_price(db):
    u = uuid.uuid4()
    g = guards.on_buy(db, **_contract(u), entry_price=Decimal("2.00"))

    assert g.sell_count == 0
    assert g.entry_price == Decimal("2.00")
    assert g.closed_at is None


def test_a_second_buy_reuses_the_same_guard(db):
    u = uuid.uuid4()
    first = guards.on_buy(db, **_contract(u), entry_price=Decimal("2.00"))
    again = guards.on_buy(db, **_contract(u), entry_price=Decimal("3.00"))
    assert again.id == first.id


def test_adding_never_re_prices_the_entry(db):
    """A later add must not move a stop that is already protecting the position.
    Re-pricing on every add would let a position that kept averaging up quietly
    raise its own stop under a trader who never asked for that."""
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u), entry_price=Decimal("2.00"))
    g = guards.on_buy(db, **_contract(u), entry_price=Decimal("5.00"))
    assert g.entry_price == Decimal("2.00")


def test_adding_does_not_reset_the_rung(db):
    """"Adding" increases size; it doesn't restart the ladder. A reset would make
    the next alert re-run rung one on a position already being worked down."""
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u), entry_price=Decimal("2.00"))
    g = guards.find(db, **_contract(u))
    guards.plan_exit(g, Decimal(4), Decimal("3.00"), CFG)       # rung 1

    guards.on_buy(db, **_contract(u), entry_price=Decimal("2.00"))   # an "Adding" alert
    assert guards.find(db, **_contract(u)).sell_count == 1


def test_a_guard_with_no_price_backfills_one(db):
    """Better to learn the reference late than never have one — without it the
    profit gate can't be evaluated at all."""
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u), entry_price=None)
    g = guards.on_buy(db, **_contract(u), entry_price=Decimal("2.50"))
    assert g.entry_price == Decimal("2.50")


# ── one guard per contract ───────────────────────────────────────────────────

def test_each_contract_is_tracked_separately(db):
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u, strike="100"), entry_price=Decimal("2.00"))
    guards.on_buy(db, **_contract(u, strike="110"), entry_price=Decimal("1.00"))

    a = guards.find(db, **_contract(u, strike="100"))
    b = guards.find(db, **_contract(u, strike="110"))
    assert a.id != b.id
    assert (a.entry_price, b.entry_price) == (Decimal("2.00"), Decimal("1.00"))


def test_calls_and_puts_are_not_the_same_contract(db):
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u, right=OptionRight.CALL), entry_price=Decimal("2"))
    guards.on_buy(db, **_contract(u, right=OptionRight.PUT), entry_price=Decimal("3"))
    assert guards.find(db, **_contract(u, right=OptionRight.PUT)).entry_price == Decimal("3")


def test_one_traders_guard_is_invisible_to_another(db):
    a, b = uuid.uuid4(), uuid.uuid4()
    guards.on_buy(db, **_contract(a), entry_price=Decimal("2.00"))
    assert guards.find(db, **_contract(b)) is None


# ── retiring ─────────────────────────────────────────────────────────────────

def test_a_retired_guard_lets_a_new_position_start_clean(db):
    """Same contract, bought again later. The old ladder must not carry over."""
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u), entry_price=Decimal("2.00"))
    old = guards.find(db, **_contract(u))
    guards.plan_exit(old, Decimal(4), Decimal("3.00"), CFG)
    guards.retire(db, old, "closed out")
    db.flush()

    fresh = guards.on_buy(db, **_contract(u), entry_price=Decimal("4.00"))
    assert fresh.id != old.id
    assert fresh.sell_count == 0
    assert fresh.entry_price == Decimal("4.00")


def test_find_ignores_retired_guards(db):
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u), entry_price=Decimal("2.00"))
    guards.retire(db, guards.find(db, **_contract(u)), "gone")
    db.flush()
    assert guards.find(db, **_contract(u)) is None


def test_a_retire_reason_is_kept_but_bounded(db):
    u = uuid.uuid4()
    guards.on_buy(db, **_contract(u), entry_price=Decimal("2.00"))
    g = guards.find(db, **_contract(u))
    guards.retire(db, g, "x" * 400)
    assert len(g.closed_reason) <= 120


# ── the entry price is the FILL, not the limit we bid ────────────────────────

class _FakeDB:
    """Just enough to resolve guard.entry_order_id -> an Order."""

    def __init__(self, order=None):
        self._order = order

    def get(self, model, key):
        return self._order if getattr(self._order, "id", None) == key else None


def _filled(price, status=None, oid=None):
    from app.models.order import OrderStatus
    o = type("O", (), {})()
    o.id = oid or uuid.uuid4()
    o.status = status or OrderStatus.FILLED
    o.filled_avg_price = Decimal(price) if price is not None else None
    return o


def test_the_entry_adopts_the_actual_fill_price():
    """The seed is the limit we BID. The +10% reprice can fill ABOVE it, and
    then every level on the ladder is measured from a price never paid: the
    -25% stop sits deeper than asked and the profit gate opens early."""
    o = _filled("0.165")
    g = DiscordPositionGuard(symbol="NIO", entry_price=Decimal("0.15"),
                             entry_order_id=o.id, sell_count=0)

    assert guards.sync_entry_price(_FakeDB(o), g) is True
    assert g.entry_price == Decimal("0.165")


def test_a_fill_at_the_bid_price_changes_nothing():
    o = _filled("2.00")
    g = DiscordPositionGuard(symbol="MSFT", entry_price=Decimal("2.00"),
                             entry_order_id=o.id, sell_count=0)
    assert guards.sync_entry_price(_FakeDB(o), g) is False
    assert g.entry_price == Decimal("2.00")


def test_an_unfilled_entry_keeps_the_provisional_price():
    """A working order has no fill to adopt. Reading 0 (or None) as the entry
    would put the whole ladder at zero."""
    from app.models.order import OrderStatus
    o = _filled(None, status=OrderStatus.SUBMITTED)
    g = DiscordPositionGuard(symbol="MSFT", entry_price=Decimal("2.00"),
                             entry_order_id=o.id, sell_count=0)
    assert guards.sync_entry_price(_FakeDB(o), g) is False
    assert g.entry_price == Decimal("2.00")


def test_a_guard_with_no_opening_order_is_left_alone():
    """Guards created before this column, and positions adopted from the
    broker. There is no order to read a fill from."""
    g = DiscordPositionGuard(symbol="MSFT", entry_price=Decimal("2.00"),
                             entry_order_id=None, sell_count=0)
    assert guards.sync_entry_price(_FakeDB(None), g) is False
    assert g.entry_price == Decimal("2.00")


def test_only_the_opening_order_can_re_price_the_entry():
    """A later add fills at its own price. Adopting that would re-average the
    reference out from under a stop already protecting the position."""
    opening = _filled("2.00")
    later_add = _filled("5.00")
    g = DiscordPositionGuard(symbol="MSFT", entry_price=Decimal("2.00"),
                             entry_order_id=opening.id, sell_count=0)

    # The add is the only order the db can see, but it is not the linked one.
    assert guards.sync_entry_price(_FakeDB(later_add), g) is False
    assert g.entry_price == Decimal("2.00")


def test_the_real_fill_is_what_the_ladder_then_measures():
    """End to end: a repriced entry that filled at 0.165 must stop at -25% of
    THAT, not of the 0.15 originally bid."""
    o = _filled("0.165")
    g = DiscordPositionGuard(symbol="NIO", entry_price=Decimal("0.15"),
                             entry_order_id=o.id, sell_count=0)
    guards.sync_entry_price(_FakeDB(o), g)

    plan = guards.plan_exit(g, Decimal(4), Decimal("0.25"), CFG)
    assert plan.sell_qty == Decimal(2)
    # 0.165 * 0.75 = 0.12375 -> 0.12 (rounded DOWN to a cent)
    assert plan.new_stop_price == Decimal("0.12")
