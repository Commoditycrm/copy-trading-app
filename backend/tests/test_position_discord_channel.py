"""The positions table shows which Discord channel opened a position.

A position is the BROKER's, not ours — there is no order id on it — so the
link has to be made by CONTRACT: for each held contract, the most recent
Discord ENTRY, and its channel.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.positions import _attach_position_channels
from app.models.order import InstrumentType, OptionRight

EXP = date(2026, 12, 18)
USER = uuid.uuid4()


class _DB:
    def __init__(self, rows):
        self._rows = rows
        self.queries = 0

    def execute(self, stmt):
        self.queries += 1
        rows = list(self._rows)
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
         right=OptionRight.CALL, label="Clint", channel="clint-alerts"):
    return (symbol, itype, expiry, Decimal(strike) if strike else None,
            right, label, channel)


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
    assert db.queries == 1
    assert all(p.discord_channel == "Clint" for p in ps)


def test_no_positions_does_not_query():
    db = _DB([])
    _attach_position_channels(db, USER, [])
    assert db.queries == 0
