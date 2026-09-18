"""Direct-Webull ``get_positions`` — option contract-term resolution.

Why this file exists (the bug it locks down)
--------------------------------------------
``WebullAdapter.get_positions`` used to build every ``BrokerPosition`` WITHOUT
``option_expiry`` / ``option_strike`` / ``option_right``. ``order_retry.
live_closeable_quantity`` matches a mirror CLOSE against those exact fields, so
on a direct-Webull subscriber account NO option position ever matched: the
broker read as FLAT, and ``copy_engine._place_mirror_with_conflict_resolve``
dropped every option trim / exit as a "dangling entry" (and cancelled the
subscriber's working entry on the way out). The subscriber was left holding a
contract the trader had already exited, with the order row saying "cancelled"
rather than "error".

So these tests assert two things end to end:
  1. an option position comes back with FULL, correct contract terms — from flat
     fields, a nested container, or the OCC symbol; and
  2. ``live_closeable_quantity`` actually MATCHES a real close request against
     what ``get_positions`` returns (the regression, stated as the thing that
     was broken rather than as an implementation detail).

All offline — a fake SDK client supplies the response bodies.

Standalone:  .venv/bin/python tests/test_webull_positions.py
Or pytest:   pytest tests/test_webull_positions.py
"""
import os
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brokers.base import BrokerOrderRequest
from app.brokers.webull import WebullAdapter
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderType
from app.services.order_retry import live_closeable_quantity


# ── fake SDK plumbing ───────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeAccountOps:
    def __init__(self, resp):
        self._resp = resp

    def get_account_position(self, account_id):
        return self._resp


class _FakeTrade:
    def __init__(self, resp):
        self.account_v2 = _FakeAccountOps(resp)


def _adapter_returning(body, status_code=200) -> WebullAdapter:
    a = WebullAdapter(
        {"app_key": "k", "app_secret": "s", "account_id": "ACC1", "region_id": "us"}
    )
    a._trade_client = lambda: _FakeTrade(_FakeResp(status_code, body))  # type: ignore[method-assign]
    return a


def _close_req(symbol="AAPL", expiry=date(2026, 6, 19), strike=Decimal("220"),
               right=OptionRight.CALL) -> BrokerOrderRequest:
    """The shape the copy engine hands live_closeable_quantity for an option close."""
    return BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION, symbol=symbol, side=OrderSide.SELL,
        order_type=OrderType.MARKET, quantity=Decimal("2"),
        option_expiry=expiry, option_strike=strike, option_right=right,
        is_closing=True,
    )


# ── stocks still work ───────────────────────────────────────────────────────
def test_stock_position_unchanged():
    a = _adapter_returning({"holdings": [{
        "symbol": "TSLA", "category": "US_STOCK", "quantity": "10",
        "cost_price": "180.25", "last_price": "190.00", "market_value": "1900",
        "unrealized_profit_loss": "97.50",
    }]})
    [p] = a.get_positions()
    assert p.instrument_type == InstrumentType.STOCK
    assert p.symbol == "TSLA" and p.quantity == Decimal("10")
    assert p.avg_entry_price == Decimal("180.25")
    assert p.option_expiry is None and p.option_strike is None


def test_short_stock_is_signed_negative():
    a = _adapter_returning({"holdings": [{
        "symbol": "TSLA", "category": "US_STOCK", "quantity": "5",
        "position_side": "SHORT",
    }]})
    [p] = a.get_positions()
    assert p.quantity == Decimal("-5")


# ── option terms: the three resolution paths ────────────────────────────────
def test_option_terms_from_flat_fields():
    a = _adapter_returning({"holdings": [{
        "symbol": "AAPL", "category": "US_OPTION", "quantity": "3",
        "strike_price": "220", "option_expire_date": "2026-06-19",
        "option_type": "CALL", "cost_price": "0.45",
    }]})
    [p] = a.get_positions()
    assert p.instrument_type == InstrumentType.OPTION
    assert p.symbol == "AAPL"                      # underlying root
    assert p.option_expiry == date(2026, 6, 19)
    assert p.option_strike == Decimal("220")
    assert p.option_right == OptionRight.CALL


def test_option_terms_from_nested_container():
    """Terms hidden under a nested option object rather than on the row."""
    a = _adapter_returning({"holdings": [{
        "symbol": "SPY", "category": "US_OPTION", "quantity": "1",
        "option": {
            "underlying_symbol": "SPY", "strikePrice": "500.5",
            "expirationDate": "2026-01-16T00:00:00Z", "optionType": "PUT",
        },
    }]})
    [p] = a.get_positions()
    assert p.symbol == "SPY"
    assert p.option_expiry == date(2026, 1, 16)
    assert p.option_strike == Decimal("500.5")
    assert p.option_right == OptionRight.PUT


def test_option_terms_from_occ_symbol_only():
    """No explicit fields at all — everything comes from the OCC ticker, and the
    reported symbol is normalised to the UNDERLYING (not the OCC string)."""
    a = _adapter_returning({"holdings": [{
        "symbol": "AAPL260619C00220000", "category": "US_OPTION", "quantity": "2",
        "instrument_id": "913256135",
    }]})
    [p] = a.get_positions()
    assert p.instrument_type == InstrumentType.OPTION
    assert p.symbol == "AAPL"                      # NOT the OCC string
    assert p.broker_symbol == "913256135"          # broker id preserved
    assert p.option_expiry == date(2026, 6, 19)
    assert p.option_strike == Decimal("220")
    assert p.option_right == OptionRight.CALL


def test_option_detected_from_occ_when_category_missing():
    """A response that omits the category is still recognised as an option."""
    a = _adapter_returning({"holdings": [
        {"symbol": "SPY260116P00500000", "quantity": "1"},
    ]})
    [p] = a.get_positions()
    assert p.instrument_type == InstrumentType.OPTION
    assert p.symbol == "SPY" and p.option_right == OptionRight.PUT


def test_occ_with_padding_spaces_parses():
    a = _adapter_returning({"holdings": [
        {"symbol": "AAPL  260619C00220000", "category": "OPTION", "quantity": "1"},
    ]})
    [p] = a.get_positions()
    assert p.symbol == "AAPL" and p.option_strike == Decimal("220")


def test_expiry_accepts_yyyymmdd_and_epoch_millis():
    a = _adapter_returning({"holdings": [
        {"symbol": "AAPL", "category": "US_OPTION", "quantity": "1",
         "strike_price": "220", "option_expire_date": "20260619", "option_type": "C"},
        {"symbol": "MSFT", "category": "US_OPTION", "quantity": "1",
         "strike_price": "400", "expiration_date": 1781827200000, "option_type": "P"},
    ]})
    aapl, msft = a.get_positions()
    assert aapl.option_expiry == date(2026, 6, 19)
    assert msft.option_expiry == date(2026, 6, 19)   # epoch millis (UTC)


def test_fractional_strike_survives():
    a = _adapter_returning({"holdings": [
        {"symbol": "SPY260116C00500500", "category": "US_OPTION", "quantity": "1"},
    ]})
    [p] = a.get_positions()
    assert p.option_strike == Decimal("500.5")


# ── unresolvable option rows are skipped, not half-populated ────────────────
def test_unresolvable_option_is_skipped_not_returned_blank():
    """A half-populated row (terms None) is indistinguishable from 'not held' to
    live_closeable_quantity, so it must NOT be returned — that's how the close
    got silently dropped. Skipping surfaces it in the logs instead."""
    a = _adapter_returning({"holdings": [
        {"symbol": "MYSTERY", "category": "US_OPTION", "quantity": "4"},
        {"symbol": "TSLA", "category": "US_STOCK", "quantity": "1"},
    ]})
    out = a.get_positions()
    assert [p.symbol for p in out] == ["TSLA"]   # the option row is gone


def test_non_200_returns_empty():
    assert _adapter_returning(None, status_code=500).get_positions() == []


# ── the regression, end to end ──────────────────────────────────────────────
def test_live_closeable_quantity_matches_the_held_option():
    """The actual bug: the copy engine asks 'how much of this contract can I
    close?' and used to get 0 for every Webull option — which it reads as
    'already flat' and drops the close."""
    a = _adapter_returning({"holdings": [{
        "symbol": "AAPL", "category": "US_OPTION", "quantity": "3",
        "strike_price": "220", "option_expire_date": "2026-06-19",
        "option_type": "CALL",
    }]})
    assert live_closeable_quantity(a, _close_req()) == Decimal("3")


def test_live_closeable_quantity_matches_occ_only_position():
    a = _adapter_returning({"holdings": [
        {"symbol": "AAPL260619C00220000", "category": "US_OPTION", "quantity": "3"},
    ]})
    assert live_closeable_quantity(a, _close_req()) == Decimal("3")


def test_live_closeable_quantity_does_not_match_a_different_contract():
    """Same underlying, different strike — must NOT be treated as closeable, or
    a close would be sized against a contract the subscriber doesn't hold."""
    a = _adapter_returning({"holdings": [
        {"symbol": "AAPL260619C00230000", "category": "US_OPTION", "quantity": "3"},
    ]})
    assert live_closeable_quantity(a, _close_req(strike=Decimal("220"))) == Decimal("0")


def test_live_closeable_quantity_does_not_match_a_different_expiry():
    a = _adapter_returning({"holdings": [
        {"symbol": "AAPL260717C00220000", "category": "US_OPTION", "quantity": "3"},
    ]})
    assert live_closeable_quantity(a, _close_req()) == Decimal("0")


def test_live_closeable_quantity_does_not_match_a_call_against_a_put():
    a = _adapter_returning({"holdings": [
        {"symbol": "AAPL260619P00220000", "category": "US_OPTION", "quantity": "3"},
    ]})
    assert live_closeable_quantity(a, _close_req(right=OptionRight.CALL)) == Decimal("0")


def test_live_closeable_quantity_for_stock_close():
    a = _adapter_returning({"holdings": [
        {"symbol": "TSLA", "category": "US_STOCK", "quantity": "10"},
    ]})
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="TSLA", side=OrderSide.SELL,
        order_type=OrderType.MARKET, quantity=Decimal("10"), is_closing=True,
    )
    assert live_closeable_quantity(a, req) == Decimal("10")


def test_buy_to_close_reads_a_short_option_position():
    """Covering a short: a BUY close must see the negative quantity as closeable."""
    a = _adapter_returning({"holdings": [{
        "symbol": "AAPL", "category": "US_OPTION", "quantity": "2",
        "position_side": "SHORT", "strike_price": "220",
        "option_expire_date": "2026-06-19", "option_type": "CALL",
    }]})
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION, symbol="AAPL", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity=Decimal("2"),
        option_expiry=date(2026, 6, 19), option_strike=Decimal("220"),
        option_right=OptionRight.CALL, is_closing=True,
    )
    assert live_closeable_quantity(a, req) == Decimal("2")


# ── the REAL payload, captured from a live account (2026-09-18) ─────────────
# Everything above was written against inferred field names. This is the actual
# response, verbatim, and it caught the one that mattered: Webull calls the
# strike `option_exercise_price` on a position leg — not `strike_price`, which is
# the ORDER-side spelling _build_option_order uses. The parser missed it, strike
# came back None, the row failed term resolution and get_positions skipped it.
#
# Symptom: the Positions page showed nothing while the account held a contract,
# and live_closeable_quantity returned 0 — which is how a mirror SELL goes out as
# SELL_TO_OPEN and the broker rejects it. Keep this fixture verbatim; it is the
# only thing here that is evidence rather than assumption.
_LIVE_POSITION_BODY = [
    {
        "currency": "USD",
        "quantity": "2",
        "cost": "30.00",
        "proportion": "1.0000",
        "legs": [
            {
                "symbol": "NIO",
                "cost": "0.15",
                "proportion": "1.0000",
                "leg_id": "81I49DTBIG560A3Q2SLVITMHM9",
                "instrument_type": "OPTION",
                "last_price": "0.15",
                "unrealized_profit_loss": "0.00",
                "day_profit_loss": "-10.57",
                "day_realized_profit_loss": "-10.57",
                "option_type": "CALL",
                "option_expire_date": "2026-09-18",
                "option_exercise_price": "3.5",
                "option_contract_multiplier": "100",
                "option_contract_deliverable": "100",
                "expiration_type": "PM",
            }
        ],
        "position_id": "81I49DTBIG560A3Q2SLVITMHM9",
        "symbol": "NIO",
        "option_strategy": "SINGLE",
        "instrument_type": "OPTION",
        "cost_price": "0.15",
        "last_price": "0.15",
        "market_value": "30.00",
        "unrealized_profit_loss": "0.00",
        "unrealized_profit_loss_rate": "0.0000",
        "day_profit_loss": "-10.57",
        "day_realized_profit_loss": "-10.57",
    }
]


def test_live_payload_parses_every_field():
    a = _adapter_returning(_LIVE_POSITION_BODY)
    [p] = a.get_positions()
    assert p.instrument_type == InstrumentType.OPTION
    assert p.symbol == "NIO"
    assert p.quantity == Decimal("2")
    assert p.option_expiry == date(2026, 9, 18)
    assert p.option_strike == Decimal("3.5")        # option_exercise_price
    assert p.option_right == OptionRight.CALL
    assert p.avg_entry_price == Decimal("0.15")
    assert p.current_price == Decimal("0.15")
    assert p.market_value == Decimal("30.00")
    assert p.unrealized_pnl == Decimal("0.00")
    assert p.cost_basis == Decimal("30.00")          # `cost`, not cost_basis
    # position_id, not the bare ticker — a ticker is not unique across the
    # option contracts of one underlying.
    assert p.broker_symbol == "81I49DTBIG560A3Q2SLVITMHM9"


def test_live_payload_is_closeable_by_the_copy_engine():
    """The regression in the terms the copy engine cares about: this returned 0
    before the fix, which is how a mirror SELL goes out as SELL_TO_OPEN."""
    a = _adapter_returning(_LIVE_POSITION_BODY)
    req = _close_req(symbol="NIO", expiry=date(2026, 9, 18),
                     strike=Decimal("3.5"), right=OptionRight.CALL)
    assert live_closeable_quantity(a, req) == Decimal("2")


def test_live_payload_does_not_match_a_neighbouring_strike():
    """3.5 must not satisfy a close for 4.0 — the clamp depends on it."""
    a = _adapter_returning(_LIVE_POSITION_BODY)
    req = _close_req(symbol="NIO", expiry=date(2026, 9, 18),
                     strike=Decimal("4.0"), right=OptionRight.CALL)
    assert live_closeable_quantity(a, req) == Decimal("0")


def test_order_side_strike_spelling_still_works():
    """strike_price is what our own order payloads use; both spellings must
    resolve, since _option_terms reads order-shaped rows too."""
    a = _adapter_returning({"holdings": [{
        "symbol": "AAPL", "category": "US_OPTION", "quantity": "1",
        "strike_price": "220", "option_expire_date": "2026-06-19",
        "option_type": "CALL",
    }]})
    [p] = a.get_positions()
    assert p.option_strike == Decimal("220")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll webull-position tests passed.")
