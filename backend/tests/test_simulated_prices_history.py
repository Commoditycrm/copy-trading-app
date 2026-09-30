"""Ladder history on the simulated-prices screen.

The screen rebuilds a guard's run of the ladder from three traces: an Order
for a rung that sold, the guard's ``armed_at`` for a rung that parked the rest
on a trailing exit, and nothing at all for a rung that only moved the stop.
Numbering has to follow what actually ran, and every entry needs an id that
survives a refresh so the page can hide what it has already shown.
"""
import inspect
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import discord_sources
from app.api.discord_sources import _ladder_history
from app.models.order import OrderStatus

T0 = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)


def _guard(sell_count=0, trail_qty=None, armed_at=None, stop="0.80"):
    return SimpleNamespace(
        id=uuid.uuid4(), created_at=T0, sell_count=sell_count,
        stop_price=Decimal(stop) if stop else None,
        trail_qty=Decimal(trail_qty) if trail_qty else None,
        armed_at=armed_at,
    )


def _sell(minutes, qty, filled, status=OrderStatus.FILLED, price="1.25"):
    at = T0 + timedelta(minutes=minutes)
    return SimpleNamespace(
        id=uuid.uuid4(), created_at=at, quantity=Decimal(qty),
        filled_quantity=Decimal(filled), filled_avg_price=Decimal(price),
        limit_price=None, status=status,
        broker_filled_at=at, closed_at=None,
    )


def test_sells_are_numbered_and_sized_in_order():
    first, second = _sell(1, "4", "4"), _sell(5, "2", "2")
    h = _ladder_history(_guard(sell_count=2), [first, second], Decimal("2"))

    assert [e.rung for e in h] == [1, 2]
    assert [(e.quantity_before, e.quantity_remaining) for e in h] == [("8", "4"), ("4", "2")]
    assert [e.id for e in h] == [str(first.id), str(second.id)]


def test_sells_from_a_previous_run_are_left_out():
    """A re-entered contract starts a new guard; the old run's trims are not its."""
    old = _sell(-30, "4", "4")
    h = _ladder_history(_guard(sell_count=1), [old, _sell(1, "2", "2")], Decimal("2"))

    assert len(h) == 1
    assert h[0].quantity_before == "4"


def test_a_trail_is_one_entry_placed_by_when_it_armed():
    """Rung 2 parked the rest on a trailing exit: no Order, only armed_at."""
    armed = T0 + timedelta(minutes=3)
    g = _guard(sell_count=2, trail_qty="2", armed_at=armed)
    h = _ladder_history(g, [_sell(1, "2", "2")], Decimal("2"))

    assert [e.status for e in h] == ["filled", "armed"]
    assert h[1].rung == 2
    assert h[1].stop_quantity == "2"
    assert h[1].happened_at == armed
    assert h[1].id == f"trail:{g.id}:{armed.isoformat()}"


def test_a_trail_id_does_not_move_when_the_guard_is_written():
    """The old code timed this entry by updated_at, which every peak_price
    update bumps — so it reappeared after the page cleared it."""
    armed = T0 + timedelta(minutes=3)
    g = _guard(sell_count=1, trail_qty="2", armed_at=armed)
    first = _ladder_history(g, [], Decimal("2"))
    g.updated_at = T0 + timedelta(hours=1)
    assert _ladder_history(g, [], Decimal("2"))[0].id == first[0].id


def test_a_stop_only_rung_is_listed_untimed_after_the_rest():
    """An alert under its gate spends the rung but only moves the stop."""
    g = _guard(sell_count=2)
    h = _ladder_history(g, [_sell(1, "2", "2")], Decimal("2"))

    assert [e.status for e in h] == ["filled", "stop_only"]
    assert h[1].happened_at is None
    assert h[1].id == f"stop:{g.id}:2"


def test_the_query_skips_rejected_trims_and_duplicate_joins():
    """A rejected trim hands its rung back, so it must not take a number; and
    a join repeats an order once per message that points at it."""
    src = inspect.getsource(discord_sources.list_simulated_prices)
    assert "Order.status != OrderStatus.REJECTED" in src
    assert "outerjoin(DiscordMessage" not in src
    assert "Order.id.in_(scissors)" in src


def test_an_old_retired_guard_is_not_borrowed():
    src = inspect.getsource(discord_sources.list_simulated_prices)
    assert "_RETIRED_GUARD_GRACE" in src
