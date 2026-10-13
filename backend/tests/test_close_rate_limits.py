"""A close from the Positions row stays inside Webull's positions-read limit.

QA 2026-10-13: one close made 9 Webull calls and 2 were refused (429). The
close cancels the Discord ladder's stop / take-profit first, but did not count
them as cancels — so when they were the only resting orders it skipped the
settle pause and the re-read, placed the sell while the contracts were still
reserved, was refused, and the conflict handling re-read positions and retried
the place three times inside two seconds. Webull allows 2 positions reads per
2 s per account, shared with the P&L poller and the Positions page.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.api import positions as mod
from app.brokers.base import BrokerPosition
from app.models.order import InstrumentType, OptionRight, OrderType

_EXP = date(2026, 10, 13)
_SYM = "SPY261013P00776000"


def _pos(qty="4"):
    return BrokerPosition(
        broker_symbol=_SYM, symbol="SPY", instrument_type=InstrumentType.OPTION,
        quantity=Decimal(qty), avg_entry_price=Decimal("0.62"),
        current_price=Decimal("0.30"), market_value=None, unrealized_pnl=None,
        option_expiry=_EXP, option_strike=Decimal("776"), option_right=OptionRight.PUT,
    )


class _Throttled(Exception):
    pass


class _Adapter:
    def __init__(self, reads):
        self._reads = list(reads)          # each: a list of positions, or an exception
        self.position_reads = 0

    def get_positions(self):
        self.position_reads += 1
        nxt = self._reads.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def clock(monkeypatch):
    """A fake clock: sleep advances monotonic time, and every pause is recorded."""
    state = {"now": 100.0, "slept": []}
    monkeypatch.setattr(mod.time, "monotonic", lambda: state["now"])

    def _sleep(s):
        state["slept"].append(s)
        state["now"] += s

    monkeypatch.setattr(mod.time, "sleep", _sleep)
    return state


def _run_close(monkeypatch, adapter, *, released, cancelled):
    acct = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), connection_status="connected",
                           encrypted_credentials="x")
    user = SimpleNamespace(id=acct.user_id)
    db = SimpleNamespace(get=lambda model, key: acct, commit=lambda: None)
    placed = []

    monkeypatch.setattr(mod, "adapter_for", lambda a, c: adapter)
    monkeypatch.setattr(mod, "decrypt_json", lambda v: {})
    from app.services import discord_stop_orders
    monkeypatch.setattr(discord_stop_orders, "release_for_position", lambda db, user, pos: released)
    monkeypatch.setattr(mod, "_cancel_working_orders_for_position",
                        lambda *a, **k: list(cancelled))
    monkeypatch.setattr(mod, "_option_close_limit", lambda adapter, pos, side: Decimal("0.29"))
    monkeypatch.setattr(mod, "_capture_exit_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_place_trader_order",
                        lambda db, user, payload, *a, **k: placed.append(payload) or payload)

    mod.close_position(
        broker_symbol=_SYM, payload=SimpleNamespace(quantity=None, order_type=OrderType.MARKET,
                                                     limit_price=None),
        request=None, background=None, broker_account_id=acct.id, db=db, user=user,
    )
    return placed


def test_a_ladder_release_alone_settles_and_re_reads(monkeypatch, clock):
    """The ladder's stop / take-profit were the only resting orders: the close
    still waits for the reservation to clear and re-reads before selling."""
    adapter = _Adapter([[_pos()], [_pos()]])
    placed = _run_close(monkeypatch, adapter, released=True, cancelled=[])
    assert adapter.position_reads == 2
    assert mod._CANCEL_SETTLE_S in clock["slept"]
    assert len(placed) == 1 and placed[0].quantity == Decimal(4)


def test_the_re_read_is_spaced_from_the_first(monkeypatch, clock):
    """Two positions reads at least _POSITIONS_READ_GAP_S apart, so the close
    never lands the third call of a burst on Webull."""
    reads_at = []
    adapter = _Adapter([[_pos()], [_pos()]])
    orig = adapter.get_positions

    def _timed():
        reads_at.append(clock["now"])
        return orig()

    adapter.get_positions = _timed
    _run_close(monkeypatch, adapter, released=True, cancelled=[])
    assert reads_at[1] - reads_at[0] >= mod._POSITIONS_READ_GAP_S


def test_a_stop_that_filled_during_the_release_is_not_sold_again(monkeypatch, clock):
    """Why the re-read matters: the ladder's stop filled before its cancel landed,
    so there is nothing left — selling anyway would open a short."""
    from fastapi import HTTPException
    adapter = _Adapter([[_pos()], []])
    with pytest.raises(HTTPException) as e:
        _run_close(monkeypatch, adapter, released=True, cancelled=[])
    assert e.value.status_code == 409


def test_nothing_resting_means_one_read_and_no_pause(monkeypatch, clock):
    adapter = _Adapter([[_pos()]])
    placed = _run_close(monkeypatch, adapter, released=False, cancelled=[])
    assert adapter.position_reads == 1
    assert clock["slept"] == []
    assert len(placed) == 1


def test_a_throttled_read_is_retried_once_after_the_window(monkeypatch, clock):
    adapter = _Adapter([
        _Throttled("HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: slow down"), [_pos()],
    ])
    placed = _run_close(monkeypatch, adapter, released=False, cancelled=[])
    assert adapter.position_reads == 2
    assert mod._RATE_LIMIT_WAIT_S in clock["slept"]
    assert len(placed) == 1


def test_other_read_errors_are_not_retried(monkeypatch, clock):
    adapter = _Adapter([RuntimeError("auth failed")])
    with pytest.raises(RuntimeError):
        _run_close(monkeypatch, adapter, released=False, cancelled=[])
    assert adapter.position_reads == 1
