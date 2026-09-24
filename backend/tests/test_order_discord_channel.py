"""The order history shows which Discord channel placed an order.

There is no channel column on Order — the link is
orders <- discord_messages.order_id -> discord_alert_sources — so it is
attached per request. One query for the whole page: a per-row lookup would be
an N+1 across a table that routinely shows hundreds of rows.
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.trades import _attach_discord_channel


class _DB:
    """Returns fixed (order_id, label, channel_name) rows, and counts queries."""

    def __init__(self, rows):
        self._rows = rows
        self.queries = 0

    def execute(self, stmt):
        self.queries += 1
        rows = list(self._rows)
        return SimpleNamespace(all=lambda: rows)


def _order():
    return SimpleNamespace(id=uuid.uuid4(), discord_channel="stale")


def test_a_discord_order_gets_its_channel():
    o = _order()
    _attach_discord_channel(_DB([(o.id, "Kopyya Testing Channel", "testing")]), [o])
    assert o.discord_channel == "Kopyya Testing Channel"


def test_the_traders_own_label_wins_over_discords_channel_name():
    """The label is what they named it and what the Discord tab shows them."""
    o = _order()
    _attach_discord_channel(_DB([(o.id, "JPM Options", "alerts-premium")]), [o])
    assert o.discord_channel == "JPM Options"


def test_it_falls_back_to_the_channel_name():
    o = _order()
    _attach_discord_channel(_DB([(o.id, None, "alerts-premium")]), [o])
    assert o.discord_channel == "alerts-premium"


def test_a_blank_label_is_not_treated_as_a_name():
    """Whitespace is not a channel name — it would render as an empty cell,
    which reads as a loading state rather than "no channel"."""
    o = _order()
    _attach_discord_channel(_DB([(o.id, "   ", None)]), [o])
    assert o.discord_channel is None


def test_a_non_discord_order_is_cleared_not_left_stale():
    """Every order is reset first. Without that, a transient value from an
    earlier attach would survive onto an order that has no channel."""
    o = _order()
    _attach_discord_channel(_DB([]), [o])
    assert o.discord_channel is None


def test_only_the_matching_orders_are_set():
    a, b = _order(), _order()
    _attach_discord_channel(_DB([(a.id, "Clint", "clint-alerts")]), [a, b])
    assert a.discord_channel == "Clint"
    assert b.discord_channel is None


def test_one_query_covers_the_whole_page():
    """The N+1 this helper exists to avoid."""
    orders = [_order() for _ in range(50)]
    db = _DB([(o.id, "Clint", "clint") for o in orders])
    _attach_discord_channel(db, orders)
    assert db.queries == 1
    assert all(o.discord_channel == "Clint" for o in orders)


def test_an_empty_page_does_not_query_at_all():
    db = _DB([])
    _attach_discord_channel(db, [])
    assert db.queries == 0
