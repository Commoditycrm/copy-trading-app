"""Per-position Day's P&L follows the connected broker's own native fields.

Webull exposes day_profit_loss (+ unrealized_profit_loss_rate); Alpaca exposes
unrealized_intraday_pl / _plpc. We map the broker's values, never a derived
figure meant to imitate another broker. Payload shapes are the real ones
captured from prod.
"""
import os
import sys
import types
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brokers.alpaca import AlpacaAdapter, _pct
from app.brokers.webull import WebullAdapter, _wb_day_pct, _wb_pct


# ── pure percent helpers ─────────────────────────────────────────────────────

def test_pct_helpers_fraction_to_percent():
    assert _pct("0.53608") == Decimal("53.608")
    assert _pct(None) is None
    assert _wb_pct("-0.3916") == Decimal("-39.16")
    # Webull day % derived from day P&L and market value (day-start = mv - day).
    assert _wb_day_pct(Decimal("-260.36"), Decimal("404.00")) == (
        Decimal("-260.36") / Decimal("664.36") * Decimal(100)
    )
    assert _wb_day_pct(None, Decimal("404")) is None


# ── Webull mapping (real payload) ────────────────────────────────────────────

def test_webull_position_maps_native_day_pnl():
    raw = {
        "symbol": "SPY", "instrument_type": "OPTION", "quantity": "8",
        "cost_price": "0.83", "last_price": "0.51", "market_value": "404.00",
        "unrealized_profit_loss": "-260.00", "unrealized_profit_loss_rate": "-0.3916",
        "day_profit_loss": "-260.36", "day_realized_profit_loss": "-0.36",
        "position_id": "P1",
        "legs": [{"symbol": "SPY", "option_type": "PUT",
                  "option_expire_date": "2026-09-30", "option_exercise_price": "766"}],
    }
    ad = WebullAdapter.__new__(WebullAdapter)  # skip __init__/SDK
    ad.account_id = "acct"
    ad._trade_client = lambda: types.SimpleNamespace(
        account_v2=types.SimpleNamespace(
            get_account_position=lambda _a: types.SimpleNamespace(
                status_code=200, json=lambda: [raw])))
    pos = ad._fetch_positions()
    assert len(pos) == 1
    p = pos[0]
    assert p.unrealized_pnl == Decimal("-260.00")          # Open P&L
    assert p.open_pnl_pct == Decimal("-39.16")             # Open P&L %
    assert p.day_pnl == Decimal("-260.36")                 # Day's P&L (native)
    assert p.day_pnl_pct is not None                       # derived


# ── Alpaca mapping (real payload) ────────────────────────────────────────────

def test_alpaca_position_maps_native_intraday_pnl():
    raw = types.SimpleNamespace(
        symbol="AMZN261016C00260000", asset_class="us_option", qty="4",
        avg_entry_price="3.75", current_price="2.98", market_value="1192",
        cost_basis="1500", unrealized_pl="-308", unrealized_plpc="-0.20533",
        unrealized_intraday_pl="416", unrealized_intraday_plpc="0.53608",
        side="long",
    )
    ad = AlpacaAdapter.__new__(AlpacaAdapter)
    ad._c = lambda: types.SimpleNamespace(get_all_positions=lambda: [raw])
    pos = ad.get_positions()
    assert len(pos) == 1
    p = pos[0]
    assert p.unrealized_pnl == Decimal("-308")             # Open P&L
    assert p.open_pnl_pct == Decimal("-20.533")            # Open P&L %
    assert p.day_pnl == Decimal("416")                     # Day's P&L (native)
    assert p.day_pnl_pct == Decimal("53.608")              # Day's P&L % (native)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
