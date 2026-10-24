"""A take-profit exit mode only counts where the BROKER can rest the pair.

discord_take_profit places its orders only on adapters with
``supports_exit_pair``; exit_mode decides whether to IGNORE a channel's trim
alerts. When those two disagree the result is silent and total: the alerts are
ignored in favour of orders that are never placed, and NOTHING trims.

Confirmed on QA 2026-10-22 — an Alpaca subscriber took 0 take-profit orders
against a Webull trader's 19, and three positions ran from entry to an -80%
stop with sell_count 0 ("position flat (rung 0)"). The Webull assertions below
are the regression guard: that broker CAN rest the pair, so nothing about it
may change.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brokers import broker_supports_exit_pair
from app.brokers.alpaca import AlpacaAdapter
from app.brokers.base import BrokerAdapter
from app.brokers.fake import FakeBrokerAdapter
from app.brokers.ibkr import IBKRAdapter
from app.brokers.snaptrade import SnapTradeAdapter
from app.models.broker_account import BrokerName
from app.services.discord_channel_settings import exit_mode


def _ts(**kw):
    base = {"discord_tp_orders": False, "discord_auto_trim": False,
            "discord_manual_exit": False}
    return SimpleNamespace(**{**base, **kw})


# ── the lookup matches the adapters it mirrors ───────────────────────────────

def test_lookup_agrees_with_every_adapter_class():
    # The lookup exists so a caller can ask without building an adapter. If it
    # ever drifts from the classes, the bug comes straight back.
    from app.brokers.webull import WebullAdapter
    for broker, cls in [
        (BrokerName.WEBULL, WebullAdapter),
        (BrokerName.ALPACA, AlpacaAdapter),
        (BrokerName.SNAPTRADE, SnapTradeAdapter),
        (BrokerName.IBKR, IBKRAdapter),
        (BrokerName.FAKE, FakeBrokerAdapter),
    ]:
        assert broker_supports_exit_pair(broker) is cls.supports_exit_pair, broker


def test_an_unknown_broker_is_not_assumed_capable():
    assert broker_supports_exit_pair(None) is False


def test_the_base_default_is_incapable():
    # A new adapter must opt IN, never inherit the mode by accident.
    assert BrokerAdapter.supports_exit_pair is False


# ── Webull: unchanged (the regression guard) ─────────────────────────────────

def test_webull_still_rests_take_profit_orders():
    assert exit_mode(_ts(discord_tp_orders=True), exit_pair=True) == "orders"


def test_webull_cut_mode_unchanged():
    ts = _ts(discord_tp_orders=True, discord_auto_trim=True)
    assert exit_mode(ts, exit_pair=True) == "orders"


# ── a broker that cannot: falls through instead of ignoring trims ────────────

def test_alpaca_falls_through_to_alerts():
    # No auto-trim: the channel's own trim alerts must be honoured again.
    assert exit_mode(_ts(discord_tp_orders=True), exit_pair=False) == "alerts"


def test_alpaca_falls_through_to_auto_trim_when_configured():
    ts = _ts(discord_tp_orders=True, discord_auto_trim=True)
    assert exit_mode(ts, exit_pair=False) == "auto"


def test_manual_still_wins_on_either_broker():
    ts = _ts(discord_tp_orders=True, discord_manual_exit=True)
    assert exit_mode(ts, exit_pair=True) == "manual"
    assert exit_mode(ts, exit_pair=False) == "manual"


# ── display callers are untouched ────────────────────────────────────────────

def test_the_default_reads_as_before():
    # A display caller names the CONFIGURED mode and has no broker to hand.
    assert exit_mode(_ts(discord_tp_orders=True)) == "orders"


def test_modes_without_take_profit_ignore_the_capability():
    for pair in (True, False):
        assert exit_mode(_ts(), exit_pair=pair) == "alerts"
        assert exit_mode(_ts(discord_auto_trim=True), exit_pair=pair) == "auto"
