"""Stops, trailing exits, trims and take-profits are judged at Alpaca's live
quote — not the broker's last mark, which on Webull costs a rate-limited
positions call and was missing whenever that call was refused."""
import uuid
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.models.order import OptionRight
from app.services import discord_auto_trim, discord_trailing_stop, live_marks, market_data_stream, price_override

EXP = date(2026, 10, 9)


def _pos(mark="1.00", strike="770", symbol="SPY"):
    return SimpleNamespace(symbol=symbol, option_strike=D(strike) if strike else None,
                           option_right=OptionRight.CALL if strike else None,
                           option_expiry=EXP if strike else None, quantity=D(2),
                           current_price=D(mark) if mark else None)


@pytest.fixture
def alpaca(monkeypatch):
    feed = {"live": {}, "rest": {}, "asked": []}
    monkeypatch.setattr(market_data_stream, "get_live_price",
                        lambda key, **k: feed["asked"].append(("live", key)) or feed["live"].get(key))
    monkeypatch.setattr(market_data_stream, "fetch_rest_quote",
                        lambda key: feed["asked"].append(("rest", key)) or feed["rest"].get(key))
    monkeypatch.setattr(price_override, "apply_to", lambda user_id, pos: None)
    return feed


def test_an_option_is_looked_up_by_its_occ_symbol(alpaca):
    alpaca["live"]["SPY261009C00770000"] = D("1.37")
    assert live_marks.position_mark(_pos()) == D("1.37")
    assert alpaca["asked"] == [("live", "SPY261009C00770000")]


def test_the_streamed_quote_wins_over_the_brokers_mark(alpaca):
    alpaca["live"]["SPY261009C00770000"] = D("1.37")
    assert live_marks.position_mark(_pos(mark="1.10")) == D("1.37")


def test_without_a_streamed_quote_one_rest_quote_is_asked(alpaca):
    alpaca["rest"]["SPY261009C00770000"] = D("1.21")
    assert live_marks.position_mark(_pos()) == D("1.21")
    assert [a[0] for a in alpaca["asked"]] == ["live", "rest"]


def test_the_brokers_mark_is_the_last_resort(alpaca):
    assert live_marks.position_mark(_pos(mark="1.10")) == D("1.10")
    assert live_marks.position_mark(_pos(mark=None)) is None


def test_a_stock_is_looked_up_by_its_ticker(alpaca):
    alpaca["live"]["AAPL"] = D("231.40")
    assert live_marks.position_mark(_pos(strike=None, symbol="aapl")) == D("231.40")


def test_a_simulated_prices_pin_still_wins(alpaca, monkeypatch):
    alpaca["live"]["SPY261009C00770000"] = D("1.37")
    monkeypatch.setattr(price_override, "apply_to", lambda user_id, pos: D("2.50"))
    assert live_marks.position_mark(_pos(), user_id=uuid.uuid4()) == D("2.50")


def test_an_alpaca_failure_falls_back_instead_of_raising(alpaca, monkeypatch):
    def _boom(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(market_data_stream, "get_live_price", _boom)
    assert live_marks.position_mark(_pos(mark="1.10")) == D("1.10")


# ── the decisions use it ─────────────────────────────────────────────────────

def test_the_stop_and_trail_engine_reads_alpaca(alpaca):
    alpaca["live"]["SPY261009C00770000"] = D("0.70")
    assert discord_trailing_stop._current_price(_pos(mark="1.10"), user_id=uuid.uuid4()) == D("0.70")


def test_auto_trim_checks_its_target_against_alpaca(alpaca):
    alpaca["live"]["SPY261009C00770000"] = D("1.50")
    guard = SimpleNamespace(symbol="SPY", option_strike=D("770"), option_right=OptionRight.CALL, option_expiry=EXP)
    assert discord_auto_trim._mark_for([_pos(mark="1.10")], guard, uuid.uuid4()) == D("1.50")


def test_auto_trim_still_needs_the_position_to_be_held(alpaca):
    alpaca["live"]["SPY261009C00770000"] = D("1.50")
    guard = SimpleNamespace(symbol="SPY", option_strike=D("770"), option_right=OptionRight.CALL, option_expiry=EXP)
    gone = _pos(); gone.quantity = D(0)
    assert discord_auto_trim._mark_for([gone], guard, uuid.uuid4()) is None
    assert discord_auto_trim._mark_for([], guard, uuid.uuid4()) is None


def test_the_engine_uses_the_tick_s_positions_instead_of_reading_again(alpaca, monkeypatch):
    """The poller already read positions for the stops this tick; the trailing
    engine reuses that read rather than spending a second Webull call."""
    from app.services import discord_position_guard as guards

    uid = uuid.uuid4()
    alpaca["live"]["SPY261009C00770000"] = D("1.30")
    guard = SimpleNamespace(user_id=uid, symbol="SPY", option_strike=D("770"), option_right="call",
                            option_expiry=EXP, stop_order_id=None, tp_stop_order_id=None,
                            stop_price=D("0.90"), trail_qty=None, trail_amount=None, peak_price=None)
    monkeypatch.setattr(guards, "armed", lambda db: [guard])

    class _Adapter:
        def get_positions(self, **kw):
            raise AssertionError("must not read positions a second time this tick")

    closed = []
    fired = discord_trailing_stop.enforce(SimpleNamespace(), uid, _Adapter(),
                                          lambda *a: closed.append(a), positions=[_pos(mark="0.50")])
    # Judged at Alpaca's 1.30 — above the 0.90 stop — not the broker's stale 0.50.
    assert fired == 0 and closed == []
