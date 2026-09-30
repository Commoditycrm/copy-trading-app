"""The positions table shows which Discord channel opened a position.

A position is the BROKER's, not ours — there is no order id on it — so the
link has to be made by CONTRACT: for each held contract, the most recent
Discord ENTRY, and its channel.
"""
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.positions import _attach_position_channels
from app.models.order import InstrumentType, OptionRight

EXP = date(2026, 12, 18)
USER = uuid.uuid4()


T0 = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)


class _DB:
    """First query: Discord entries, newest first. Second: live exit ladders
    as (symbol, strike, right, expiry, created_at, entry order placed_at)."""

    def __init__(self, rows, guards=()):
        self._results = [list(rows), list(guards)]
        self.queries = 0

    def execute(self, stmt):
        rows = self._results[self.queries] if self.queries < 2 else []
        self.queries += 1
        return SimpleNamespace(all=lambda: rows)


def _pos(symbol="MSFT", itype=InstrumentType.OPTION, strike="100",
         right=OptionRight.CALL, expiry=EXP):
    return SimpleNamespace(
        symbol=symbol, instrument_type=itype,
        option_strike=Decimal(strike) if strike else None,
        option_right=right, option_expiry=expiry,
        discord_channel="stale",
    )


def _row(symbol="MSFT", itype=InstrumentType.OPTION, expiry=EXP, strike="100",
         right=OptionRight.CALL, label="Clint", channel="clint-alerts",
         channel_id="123", at=T0):
    return (symbol, itype, expiry, Decimal(strike) if strike else None,
            right, label, channel, channel_id, at)


def _self(at, **kw):
    return _row(label="Self", channel="Self", channel_id="self", at=at, **kw)


def _guard(entry_at=T0, symbol="MSFT", strike="100", right="call", expiry=EXP):
    return (symbol, Decimal(strike), right, expiry, entry_at, entry_at)


def test_a_discord_opened_position_shows_its_channel():
    p = _pos()
    _attach_position_channels(_DB([_row()]), USER, [p])
    assert p.discord_channel == "Clint"


def test_a_position_opened_elsewhere_shows_nothing():
    """Trade panel, copy mirror, or bought in the broker's own app."""
    p = _pos()
    _attach_position_channels(_DB([]), USER, [p])
    assert p.discord_channel is None


def test_the_most_recent_entry_wins():
    """Re-entering the same contract from a different channel should show the
    channel that opened what you hold NOW, not the first that ever traded it.
    The query is ordered newest-first, so the first row seen wins."""
    p = _pos()
    rows = [_row(label="Zenith"), _row(label="Clint")]   # newest first
    _attach_position_channels(_DB(rows), USER, [p])
    assert p.discord_channel == "Zenith"


def test_a_different_strike_does_not_match():
    """Matching is per CONTRACT, not per symbol — two MSFT calls at different
    strikes can come from different channels."""
    p = _pos(strike="100")
    _attach_position_channels(_DB([_row(strike="200")]), USER, [p])
    assert p.discord_channel is None


def test_a_different_expiry_does_not_match():
    p = _pos(expiry=EXP)
    _attach_position_channels(_DB([_row(expiry=date(2027, 1, 15))]), USER, [p])
    assert p.discord_channel is None


def test_a_stock_position_matches_on_symbol_alone():
    p = _pos(symbol="AAPL", itype=InstrumentType.STOCK, strike=None,
             right=None, expiry=None)
    row = _row(symbol="AAPL", itype=InstrumentType.STOCK, expiry=None,
               strike=None, right=None, label="Swings")
    _attach_position_channels(_DB([row]), USER, [p])
    assert p.discord_channel == "Swings"


def test_it_falls_back_to_the_raw_channel_name():
    p = _pos()
    _attach_position_channels(_DB([_row(label=None, channel="clint-alerts")]), USER, [p])
    assert p.discord_channel == "clint-alerts"


def test_one_query_covers_every_account():
    """This endpoint is called up to four times per order event — a per-row
    lookup would be an N+1 on every refresh."""
    ps = [_pos(strike=str(s)) for s in range(100, 130)]
    db = _DB([_row(strike=str(s)) for s in range(100, 130)])
    _attach_position_channels(db, USER, ps)
    assert db.queries == 2          # entries + live ladders, however many rows
    assert all(p.discord_channel == "Clint" for p in ps)


def test_no_positions_does_not_query():
    db = _DB([])
    _attach_position_channels(db, USER, [])
    assert db.queries == 0


def test_a_query_failure_does_not_break_the_positions_endpoint():
    """This endpoint is how a trader CLOSES a position. The unreachable-account
    handling exists so one bad broker cannot blank the list; a display column
    must not undo that for a different reason."""
    import app.api.positions as mod

    class _Broken:
        def execute(self, stmt):
            raise RuntimeError("database hiccup")

    import inspect
    import re

    src = inspect.getsource(mod.list_positions)
    # The CALL has to be inside a try. Checking merely that the function
    # contains "except Exception" proves nothing — it already has several, for
    # the unreachable-broker handling.
    guarded = re.search(
        r"try:\s*\n\s*_attach_position_channels\([^\n]*\)\s*\n\s*except Exception",
        src,
    )
    assert guarded, "the channel attach must not be able to 500 the positions endpoint"


# ── Self on top of another channel ──────────────────────────────────────────

def test_self_added_to_a_channels_position_reads_channel_dash_self():
    """Clint opened it; the trader added through Self. Both are in it."""
    p = _pos()
    rows = [_self(T0 + timedelta(minutes=30)), _row(at=T0)]
    _attach_position_channels(_DB(rows, [_guard(T0)]), USER, [p])
    assert p.discord_channel == "Clint-Self"


def test_the_channel_that_opened_the_holding_is_named():
    p = _pos()
    rows = [_self(T0 + timedelta(minutes=30)),
            _row(label="Zenith", at=T0 + timedelta(minutes=10)),
            _row(label="Clint", at=T0)]
    _attach_position_channels(_DB(rows, [_guard(T0)]), USER, [p])
    assert p.discord_channel == "Clint-Self"


def test_self_alone_reads_self():
    p = _pos()
    _attach_position_channels(_DB([_self(T0)], [_guard(T0)]), USER, [p])
    assert p.discord_channel == "Self"


def test_a_channel_alone_is_unchanged():
    p = _pos()
    _attach_position_channels(_DB([_row(at=T0)], [_guard(T0)]), USER, [p])
    assert p.discord_channel == "Clint"


def test_a_channel_entry_from_an_earlier_holding_is_not_joined():
    """Clint traded this contract last week and it was closed; today's holding
    was opened by Self. It is Self's alone."""
    p = _pos()
    today = T0 + timedelta(days=7)
    rows = [_self(today), _row(at=T0)]
    _attach_position_channels(_DB(rows, [_guard(today)]), USER, [p])
    assert p.discord_channel == "Self"


def test_without_a_live_ladder_the_most_recent_entry_is_shown():
    """No ladder, no way to tell holdings apart — nothing is combined."""
    p = _pos()
    rows = [_self(T0 + timedelta(minutes=30)), _row(at=T0)]
    _attach_position_channels(_DB(rows), USER, [p])
    assert p.discord_channel == "Self"


def test_the_ladder_is_matched_per_contract():
    p = _pos(strike="100")
    rows = [_self(T0 + timedelta(minutes=30)), _row(at=T0)]
    _attach_position_channels(_DB(rows, [_guard(T0, strike="200")]), USER, [p])
    assert p.discord_channel == "Self"
