"""A stop the market already crossed is reported as what happened.

QA 2026-10-12 / 10-16 / 10-19: after a take-profit filled, the ladder raised the
stop on what was left, the price fell through it before it rested, and Webull
refused it ("Stop price must be less than market price for a sell order (0.3)").
The app sold at market a second later — but the trader's notification only said
"Your SELL … order was rejected", which read as a failure.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.services import discord_stop_orders as dso

WEBULL = ("HTTP Status: 417, Code: OPENAPI_STOP_PRICE_MUST_BE_LESS_THAN_MARKET_PRICE, "
          "Msg: Stop price must be less than market price for a sell order（0.3）, RequestID: c4e0")


def _guard():
    return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), symbol="IWM", option_strike=Decimal("271"),
                           option_right="put", option_expiry=date(2026, 10, 19), stop_order_id=None)


@pytest.mark.parametrize("msg, crossed", [
    (WEBULL, True),
    ("stop price must be less than current price", True),
    ("insufficient position", False),
    ("This contract does not support stop orders", False),
    ("Too Many Requests", False),
])
def test_what_counts_as_crossed(msg, crossed):
    assert dso.is_stop_crossed(msg) is crossed


@pytest.fixture
def notices(monkeypatch):
    sent = []
    from app.services import notifications
    monkeypatch.setattr(notifications, "create_notification",
                        lambda db, **kw: sent.append(kw))
    monkeypatch.setattr(dso, "working_exit", lambda db, g: None)
    monkeypatch.setattr("app.services.market_hours.in_regular_session", lambda *a, **k: True)
    return sent


def _refuse(msg):
    def place(q, p):
        raise RuntimeError(msg)
    return place


def test_a_crossed_stop_sells_and_says_so(notices):
    sold = []
    out = dso._place_or_close(None, _guard(), Decimal(4), Decimal("0.31"), _refuse(WEBULL),
                              lambda q: sold.append(q))
    assert out is None and sold == [Decimal(4)]
    assert len(notices) == 1
    n = notices[0]
    assert n["type"] == "discord.stop_crossed"
    assert n["message"] == ("IWM P $271 19 Oct 26: the stop at 0.31 was already crossed "
                            "(market 0.3) — sold 4 at market.")


def test_outside_the_session_it_says_when_it_will_sell(notices, monkeypatch):
    monkeypatch.setattr("app.services.market_hours.in_regular_session", lambda *a, **k: False)
    dso._place_or_close(None, _guard(), Decimal(4), Decimal("0.31"), _refuse(WEBULL), lambda q: None)
    assert "when the regular session opens" in notices[0]["message"]


def test_any_other_refusal_gets_no_crossed_notice(notices):
    dso._place_or_close(None, _guard(), Decimal(4), Decimal("0.31"),
                        _refuse("This contract does not support stop orders"), lambda q: None)
    assert notices == []


def test_the_ladder_stop_placer_silences_only_the_crossed_rejection():
    import inspect
    from app.services import pnl_poller
    src = inspect.getsource(pnl_poller._make_stop_placer)
    assert "quiet_rejection=discord_stop_orders.is_stop_crossed" in src


def test_the_order_path_skips_its_notice_when_told_to():
    import inspect
    from app.api import trades
    src = inspect.getsource(trades._place_trader_order)
    assert "if quiet_rejection is not None and quiet_rejection(str(exc)):" in src
