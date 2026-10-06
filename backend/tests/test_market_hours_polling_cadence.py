"""Background pollers back off when the US market is closed and run at full
cadence during regular + extended hours. Copy-trading/risk cadence during
tradable hours is unchanged; off-hours we cut CPU + broker REST calls.
"""
import os
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zoneinfo import ZoneInfo

from app.config import get_settings
from app.models.broker_account import BrokerName
from app.services import market_hours, pnl_poller, webull_listener

ET = ZoneInfo("America/New_York")


def _et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def test_is_tradable_now_windows():
    # Weekday 2026-10-06 (Tuesday).
    assert market_hours.is_tradable_now(_et(2026, 10, 6, 10, 0)) is True   # regular
    assert market_hours.is_tradable_now(_et(2026, 10, 6, 5, 0)) is True    # pre-market
    assert market_hours.is_tradable_now(_et(2026, 10, 6, 18, 0)) is True   # post-market
    assert market_hours.is_tradable_now(_et(2026, 10, 6, 2, 0)) is False   # overnight
    assert market_hours.is_tradable_now(_et(2026, 10, 6, 21, 0)) is False  # after extended
    # Weekend 2026-10-10 is a Saturday.
    assert market_hours.is_tradable_now(_et(2026, 10, 10, 11, 0)) is False


def test_pnl_poller_full_cadence_when_tradable(monkeypatch):
    monkeypatch.setattr(market_hours, "is_tradable_now", lambda *a, **k: True)
    # Alpaca default 10s, Webull static 15s — unchanged while tradable.
    assert pnl_poller._interval_for_broker(BrokerName.ALPACA) == 10.0
    assert pnl_poller._interval_for_broker(BrokerName.WEBULL) == 15.0


def test_pnl_poller_backs_off_when_closed(monkeypatch):
    monkeypatch.setattr(market_hours, "is_tradable_now", lambda *a, **k: False)
    closed = float(get_settings().pnl_poll_interval_closed_seconds)
    # Both brokers floored to the closed interval (>= their full-cadence value).
    assert pnl_poller._interval_for_broker(BrokerName.ALPACA) == closed
    assert pnl_poller._interval_for_broker(BrokerName.WEBULL) == closed


def test_webull_poll_full_cadence_when_tradable(monkeypatch):
    monkeypatch.setattr(webull_listener.market_hours, "is_tradable_now", lambda *a, **k: True)
    assert webull_listener._effective_poll_interval(1, uuid.uuid4()) == webull_listener._safe_poll_interval(1)


def test_webull_poll_slow_when_closed_idle(monkeypatch):
    monkeypatch.setattr(webull_listener.market_hours, "is_tradable_now", lambda *a, **k: False)
    monkeypatch.setattr(webull_listener, "_has_working_orders", lambda uid: False)
    assert webull_listener._effective_poll_interval(1, uuid.uuid4()) == \
        float(get_settings().webull_poll_interval_closed_seconds)


def test_webull_poll_faster_when_closed_with_working_orders(monkeypatch):
    monkeypatch.setattr(webull_listener.market_hours, "is_tradable_now", lambda *a, **k: False)
    monkeypatch.setattr(webull_listener, "_has_working_orders", lambda uid: True)
    got = webull_listener._effective_poll_interval(1, uuid.uuid4())
    assert got == float(get_settings().webull_poll_interval_closed_active_seconds)
    # faster than idle, still >= the rate-limit floor
    assert got < float(get_settings().webull_poll_interval_closed_seconds)
    assert got >= webull_listener._safe_poll_interval(1)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
