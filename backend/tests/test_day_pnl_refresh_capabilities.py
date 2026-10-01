"""Broker-aware Day P&L refresh cadence is declared in capabilities, NOT
hardcoded per broker name in the app. Alpaca = 10s (reuses the runtime knob),
Webull = 30s. Neither pushes the authoritative account Day P&L, so both poll.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brokers import capabilities as caps
from app.brokers.capabilities import capabilities_for, effective_day_pnl_interval_s
from app.models.broker_account import BrokerName


def test_no_broker_claims_account_pnl_push():
    # Alpaca's TradingStream / our SSE are a refresh TRIGGER, not the P&L source.
    for b in (BrokerName.ALPACA, BrokerName.WEBULL, BrokerName.SNAPTRADE):
        assert capabilities_for(b).account_pnl_push is False


def test_recommended_intervals():
    assert capabilities_for(BrokerName.ALPACA).recommended_refresh_interval_s == 10
    assert capabilities_for(BrokerName.WEBULL).recommended_refresh_interval_s == 30
    # Unmapped / disconnected → the safe 30s default.
    assert capabilities_for(None).recommended_refresh_interval_s == 30


def test_alpaca_effective_interval_reuses_runtime_knob(monkeypatch):
    # The effective Alpaca value comes from the existing admin/runtime knob, so
    # there's ONE Alpaca interval across backend polling and the UI.
    import app.services.platform_config as pc
    monkeypatch.setattr(pc, "get_alpaca_pnl_poll_interval_sync", lambda: 7)
    assert effective_day_pnl_interval_s(BrokerName.ALPACA) == 7


def test_alpaca_effective_interval_falls_back_when_knob_unavailable(monkeypatch):
    import app.services.platform_config as pc
    def _boom():
        raise RuntimeError("redis down")
    monkeypatch.setattr(pc, "get_alpaca_pnl_poll_interval_sync", _boom)
    # Falls back to the static recommended value, never crashes.
    assert effective_day_pnl_interval_s(BrokerName.ALPACA) == 10


def test_webull_effective_interval_is_static():
    assert effective_day_pnl_interval_s(BrokerName.WEBULL) == 30


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
