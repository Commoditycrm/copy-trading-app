"""Webull positions shared across processes (QA 2026-10-01: "Rate limited by
the broker — retrying. Positions from this account are not shown").

Every live read is saved; a display read reuses one up to 10s old from any
process; on a 429 the last read (up to 5 min) is shown, marked with its age,
instead of nothing. A fill / close stops it being reused as fresh.
"""
import os
import sys
import time
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.brokers import webull as wb
from app.brokers.base import BrokerPosition
from app.models.order import InstrumentType, OptionRight


class _Redis(dict):
    def set(self, k, v, ex=None, keepttl=False): self[k] = v
    def get(self, k): return dict.get(self, k)


def _pos(qty="2"):
    return BrokerPosition(broker_symbol="SPY261002P00765000", symbol="SPY",
                          instrument_type=InstrumentType.OPTION, quantity=Decimal(qty),
                          avg_entry_price=Decimal("1.10"), current_price=Decimal("1.25"),
                          market_value=Decimal("250"), unrealized_pnl=Decimal("30"),
                          option_expiry=date(2026, 10, 2), option_strike=Decimal("765"),
                          option_right=OptionRight.PUT)


@pytest.fixture
def world(monkeypatch):
    r = _Redis()
    monkeypatch.setattr("app.services.redis_client.get_sync_redis", lambda: r)
    wb._positions_cache.clear(); wb._positions_locks.clear()
    ad = wb.WebullAdapter.__new__(wb.WebullAdapter)
    ad.app_key, ad.account_id = "KEY", "ACC"
    calls = {"n": 0, "fail": None}

    def _fetch():
        calls["n"] += 1
        if calls["fail"]:
            raise RuntimeError(calls["fail"])
        return [_pos()]

    ad._fetch_positions = _fetch
    yield ad, calls, r
    wb._positions_cache.clear(); wb._positions_locks.clear()


def test_a_read_round_trips_through_the_snapshot(world):
    ad, calls, r = world
    ad.get_positions()                                   # a decision-path live read saves it
    age, fresh, got = wb._snapshot_read("KEY", "ACC")
    assert fresh and age < 5 and got == [_pos()]


def test_another_process_reuses_a_fresh_read(world):
    ad, calls, r = world
    ad.get_positions()                                   # e.g. the worker's P&L poller
    wb._positions_cache.clear()                          # the web process has its own cache
    assert ad.get_positions(cached_ok=True) == [_pos()]
    assert calls["n"] == 1                               # no second Webull call


def test_an_old_snapshot_is_not_reused(world, monkeypatch):
    ad, calls, r = world
    ad.get_positions()
    wb._positions_cache.clear()
    real = time.time
    monkeypatch.setattr(wb.time, "time", lambda: real() + 30)
    ad.get_positions(cached_ok=True)
    assert calls["n"] == 2


def test_a_rate_limit_shows_the_last_positions_with_their_age(world, monkeypatch):
    ad, calls, r = world
    ad.get_positions()
    wb._positions_cache.clear()
    real = time.time
    monkeypatch.setattr(wb.time, "time", lambda: real() + 40)
    calls["fail"] = "HTTP Status: 429, Code: TOO_MANY_REQUESTS"
    out = ad.get_positions(cached_ok=True)
    assert out == [_pos()] and 39 <= out.stale_age_s <= 45


def test_a_rate_limit_with_no_snapshot_still_reports_the_failure(world):
    ad, calls, r = world
    calls["fail"] = "HTTP Status: 429, Code: TOO_MANY_REQUESTS"
    with pytest.raises(RuntimeError):
        ad.get_positions(cached_ok=True)


def test_other_errors_are_not_hidden(world):
    ad, calls, r = world
    ad.get_positions()
    wb._positions_cache.clear()
    wb._snapshot_mark_stale("KEY", "ACC")
    calls["fail"] = "HTTP Status: 401, unauthorized"
    with pytest.raises(RuntimeError):
        ad.get_positions(cached_ok=True)


def test_a_fill_stops_the_snapshot_being_reused_as_fresh(world):
    ad, calls, r = world
    ad.get_positions()
    wb.invalidate_positions_cache("KEY", "ACC")
    ad.get_positions(cached_ok=True)
    assert calls["n"] == 2


def test_the_app_key_is_not_in_the_redis_key(world):
    ad, calls, r = world
    ad.get_positions()
    assert all("KEY" not in k for k in r)
