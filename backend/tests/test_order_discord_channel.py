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
    """Returns fixed (order_id, label, channel_name, channel_id) rows, and
    counts queries."""

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
    _attach_discord_channel(_DB([(o.id, "Kopyya Testing Channel", "testing", "1")]), [o])
    assert o.discord_channel == "Kopyya Testing Channel"


def test_the_traders_own_label_wins_over_discords_channel_name():
    """The label is what they named it and what the Discord tab shows them."""
    o = _order()
    _attach_discord_channel(_DB([(o.id, "JPM Options", "alerts-premium", "1")]), [o])
    assert o.discord_channel == "JPM Options"


def test_it_falls_back_to_the_channel_name():
    o = _order()
    _attach_discord_channel(_DB([(o.id, None, "alerts-premium", "1")]), [o])
    assert o.discord_channel == "alerts-premium"


def test_a_blank_label_is_not_treated_as_a_name():
    """Whitespace is not a channel name — it would render as an empty cell,
    which reads as a loading state rather than "no channel"."""
    o = _order()
    _attach_discord_channel(_DB([(o.id, "   ", None, "1")]), [o])
    assert o.discord_channel is None


def test_a_non_discord_order_is_cleared_not_left_stale():
    """Every order is reset first. Without that, a transient value from an
    earlier attach would survive onto an order that has no channel."""
    o = _order()
    _attach_discord_channel(_DB([]), [o])
    assert o.discord_channel is None


def test_only_the_matching_orders_are_set():
    a, b = _order(), _order()
    _attach_discord_channel(_DB([(a.id, "Clint", "clint-alerts", "1")]), [a, b])
    assert a.discord_channel == "Clint"
    assert b.discord_channel is None


def test_one_query_covers_the_whole_page():
    """The N+1 this helper exists to avoid."""
    orders = [_order() for _ in range(50)]
    db = _DB([(o.id, "Clint", "clint", "1") for o in orders])
    _attach_discord_channel(db, orders)
    assert db.queries == 1
    assert all(o.discord_channel == "Clint" for o in orders)


def test_an_empty_page_does_not_query_at_all():
    db = _DB([])
    _attach_discord_channel(db, [])
    assert db.queries == 0


# ── a display column must not be able to break the table ────────────────────

class _BrokenDB:
    def execute(self, stmt):
        raise RuntimeError("database hiccup")


def test_a_query_failure_leaves_the_column_blank_not_the_table_broken():
    """The order history is how a trader sees and cancels working orders. A
    decorative column failing must not take that down."""
    o = _order()
    _attach_discord_channel(_BrokenDB(), [o])     # must not raise
    assert o.discord_channel is None


# ── a CLOSE shows the channel of the position it closed ──────────────────────
# A close from the Positions page has no alert behind it, and an auto-trim
# fires through Self — on their own those read blank / "Self".

from datetime import datetime, timedelta, timezone   # noqa: E402
from decimal import Decimal                          # noqa: E402

T0 = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)
USER = uuid.uuid4()


class _SeqDB:
    """Answers the three queries in order: the orders' own alerts, the Discord
    entries on those contracts (newest first), hand-assigned ladders."""

    def __init__(self, own=(), entries=(), assigned=()):
        self._answers = [list(own), list(entries), list(assigned)]
        self.queries = 0

    def execute(self, stmt):
        rows = self._answers[self.queries] if self.queries < 3 else []
        self.queries += 1
        return SimpleNamespace(all=lambda: rows)


def _close(minutes=30, closing=True):
    at = T0 + timedelta(minutes=minutes)
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=USER, symbol="SPY", option_strike=Decimal("764"),
        option_right="call", option_expiry=None, is_closing=closing,
        submitted_at=at, created_at=at, discord_channel="stale",
    )


def _entry(label, minutes):
    return (USER, "SPY", Decimal("764"), "call", None, label, label.lower(), T0 + timedelta(minutes=minutes))


def test_a_close_from_the_positions_page_shows_the_opening_channel():
    o = _close()
    _attach_discord_channel(_SeqDB(entries=[_entry("Clint", 0)]), [o])
    assert o.discord_channel == "Clint"


def test_an_auto_trim_through_self_shows_the_opening_channel():
    o = _close()
    db = _SeqDB(own=[(o.id, "Self", "Self", "self")], entries=[_entry("Clint", 0)])
    _attach_discord_channel(db, [o])
    assert o.discord_channel == "Clint"


def test_a_close_from_a_channels_own_exit_alert_keeps_that_channel():
    o = _close()
    db = _SeqDB(own=[(o.id, "Julia", "julia", "9")], entries=[_entry("Clint", 0)])
    _attach_discord_channel(db, [o])
    assert o.discord_channel == "Julia" and db.queries == 1


def test_the_entry_must_come_before_the_close():
    """A later re-entry from another channel is not what this close closed."""
    o = _close(minutes=30)
    db = _SeqDB(entries=[_entry("Julia", 45), _entry("Clint", 0)])     # newest first
    _attach_discord_channel(db, [o])
    assert o.discord_channel == "Clint"


def test_a_hand_assigned_channel_wins_for_the_close():
    o = _close(minutes=30)
    assigned = [(USER, "SPY", Decimal("764"), "call", None, T0, None, "Julia", "julia")]
    _attach_discord_channel(_SeqDB(entries=[_entry("Clint", 0)], assigned=assigned), [o])
    assert o.discord_channel == "Julia"


def test_an_assignment_from_an_earlier_holding_is_not_used():
    o = _close(minutes=120)
    ended = T0 + timedelta(minutes=20)                                  # retired long before
    assigned = [(USER, "SPY", Decimal("764"), "call", None, T0, ended, "Julia", "julia")]
    _attach_discord_channel(_SeqDB(entries=[_entry("Clint", 0)], assigned=assigned), [o])
    assert o.discord_channel == "Clint"


def test_a_close_with_no_discord_entry_stays_blank():
    o = _close()
    _attach_discord_channel(_SeqDB(), [o])
    assert o.discord_channel is None


def test_an_entry_never_borrows_a_channel():
    o = _close(closing=False)
    db = _SeqDB(entries=[_entry("Clint", 0)])
    _attach_discord_channel(db, [o])
    assert o.discord_channel is None and db.queries == 1
