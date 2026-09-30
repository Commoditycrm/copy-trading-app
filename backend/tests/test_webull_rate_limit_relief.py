"""Keeping a Webull app key out of its rate limit.

1. A failed sign-in is remembered: callers fail fast during a back-off instead
   of all re-running Webull's token flow at once. Connect/activate clears it.
2. The paper sandbox's 404 on today-orders pauses that account's poll and does
   not throw the signed-in client away (which re-ran the sign-in every cycle).
3. Prices come from the Alpaca data account first; Webull's quote API is only
   the fallback.
"""
import os
import sys
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.brokers import webull as wb
from app.services import market_data_stream as mds
from app.services import webull_listener as wl


# ── 1. sign-in back-off ─────────────────────────────────────────────────────

@pytest.fixture
def signin(monkeypatch):
    import webull.trade.trade_client as tc

    calls = SimpleNamespace(n=0, fail=True)

    class _Client:
        def __init__(self, api_client):
            calls.n += 1
            if calls.fail:
                raise RuntimeError("HTTP Status: 429, Code: TOO_MANY_REQUESTS")

    monkeypatch.setattr(tc, "TradeClient", _Client)
    monkeypatch.setattr(wb, "set_per_account_token_dir", lambda *a, **k: None)
    wb._trade_clients.pop("KEY", None)
    wb._signin_backoff.pop("KEY", None)
    yield calls
    wb._trade_clients.pop("KEY", None)
    wb._signin_backoff.pop("KEY", None)


def test_a_failed_sign_in_backs_off_instead_of_retrying(signin):
    with pytest.raises(RuntimeError, match="TOO_MANY"):
        wb.trade_client_for("KEY", "SECRET", paper=True)
    for _ in range(5):                      # the poller, auto-trim, balance refresh …
        with pytest.raises(wb.WebullSignInBackoff):
            wb.trade_client_for("KEY", "SECRET", paper=True)
    assert signin.n == 1                    # Webull was asked once, not six times


def test_the_back_off_grows_and_caps(signin):
    import time

    waits = []
    for _ in range(6):
        with pytest.raises(RuntimeError):
            wb.trade_client_for("KEY", "SECRET", paper=True)
        retry_at, count = wb._signin_backoff["KEY"]
        waits.append(round(retry_at - time.monotonic()))
        wb._signin_backoff["KEY"] = (0.0, count)          # let the next attempt through
    assert waits == [60, 120, 240, 480, 900, 900]


def test_connecting_clears_the_back_off(signin):
    with pytest.raises(RuntimeError):
        wb.trade_client_for("KEY", "SECRET", paper=True)
    wb.clear_sign_in_backoff("KEY")
    signin.fail = False
    assert wb.trade_client_for("KEY", "SECRET", paper=True) is not None
    assert "KEY" not in wb._signin_backoff


# ── 2. the sandbox's missing today-orders route ─────────────────────────────

@pytest.fixture
def poll(monkeypatch):
    state = SimpleNamespace(exc=None, invalidated=0)

    class _Order:
        def list_today_orders(self, account_id, page_size=100):
            raise state.exc

    monkeypatch.setattr(wl, "_webull_trade_client", lambda creds: SimpleNamespace(order=_Order()))
    monkeypatch.setattr(wl, "_invalidate_trade_client",
                        lambda creds: setattr(state, "invalidated", state.invalidated + 1))
    wl._dayorders_unavailable.pop("ACC", None)
    yield state
    wl._dayorders_unavailable.pop("ACC", None)


def test_a_404_pauses_the_account_and_keeps_the_client(poll):
    poll.exc = RuntimeError("HTTP Status: 404, Code: SDK.UnknownServerError, Msg: 404 Route Not Found")
    assert wl._list_today_orders({"app_key": "K"}, "ACC") == []
    assert wl._dayorders_paused("ACC")
    assert poll.invalidated == 0


def test_a_rate_limit_keeps_the_client(poll):
    poll.exc = RuntimeError("HTTP Status: 429, Code: TOO_MANY_REQUESTS")
    wl._list_today_orders({"app_key": "K"}, "ACC")
    assert poll.invalidated == 0 and not wl._dayorders_paused("ACC")


def test_a_server_blip_keeps_the_client(poll):
    poll.exc = RuntimeError("HTTP Status: 502, Bad Gateway")
    wl._list_today_orders({"app_key": "K"}, "ACC")
    assert poll.invalidated == 0


def test_only_a_sign_in_error_rebuilds_the_client(poll):
    poll.exc = RuntimeError("HTTP Status: 401, Code: UNAUTHORIZED, token expired")
    wl._list_today_orders({"app_key": "K"}, "ACC")
    assert poll.invalidated == 1


# ── 3. prices from the Alpaca data account first ────────────────────────────

class _NoWebullData:
    def __getattr__(self, name):
        raise AssertionError("Webull's quote API was called although Alpaca had a price")


def _adapter():
    ad = wb.WebullAdapter.__new__(wb.WebullAdapter)
    ad.app_key = "KEY"
    ad._data_client = lambda: _NoWebullData()
    return ad


def test_a_stock_price_comes_from_alpaca_first(monkeypatch):
    monkeypatch.setattr(mds, "data_stock_price", lambda sym: Decimal("333.90"))
    assert _adapter().get_stock_latest_price("AAPL") == Decimal("333.90")


def test_an_option_quote_comes_from_alpaca_first(monkeypatch):
    monkeypatch.setattr(mds, "data_option_bid_ask", lambda occ: (Decimal("1.10"), Decimal("1.15")))
    assert _adapter().get_option_latest_quote("SPY261002P00765000") == (Decimal("1.10"), Decimal("1.15"))


def test_webull_is_the_fallback_when_alpaca_has_nothing(monkeypatch):
    monkeypatch.setattr(mds, "data_stock_price", lambda sym: None)
    ad = _adapter()
    ad._quotes_available = lambda: True
    snap = SimpleNamespace(status_code=200, json=lambda: [{"last_price": "334.10"}])
    ad._data_client = lambda: SimpleNamespace(market_data=SimpleNamespace(get_snapshot=lambda *a, **k: snap))
    assert ad.get_stock_latest_price("AAPL") == Decimal("334.10")


def test_no_data_key_means_no_alpaca_option_call(monkeypatch):
    from app import config

    monkeypatch.setattr(config.get_settings(), "alpaca_data_api_key", "")
    assert mds.data_option_bid_ask("SPY261002P00765000") == (None, None)
