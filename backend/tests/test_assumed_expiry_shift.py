"""An expiry WE assumed may be corrected; one the trader wrote may not.

An entry that names a contract and a price but no date is read as a same-day
trade — right for the channels that post "SPY 763c @ .55" and never write a
date. But a same-day read only works where the underlying has a same-day
expiry: NVDA has Friday weeklies, so "NVDA 215P 1.4" posted on a Thursday
asked the broker for a contract that has never existed (QA 2026-10-22, Webull
OPENAPI_PARAM_ERR). The date was our guess, so execution corrects it.

The asymmetry is the whole point, and the refusal tests below are what hold it:
a date the TRADER stated is still refused rather than moved, because silently
shifting it buys a contract they did not ask for.
"""
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_execution as ex
from app.models.order import OptionRight
from app.services.discord_parsers import parse_message
from app.services.discord_parsers.base import ParsedMessage

# 2026-10-22 is a Thursday; NVDA lists no Thursday expiry.
THURSDAY = datetime(2026, 10, 22, 13, 31, tzinfo=timezone.utc)
FRIDAY = date(2026, 10, 23)


def _contract(strike, right, exp):
    return SimpleNamespace(strike_price=strike, type=SimpleNamespace(value=right),
                           expiration_date=exp)


def _chain(*contracts):
    """An adapter that honours the expiry window the caller asks for."""
    def list_option_contracts(**kw):
        lo, hi = kw.get("expiry_gte"), kw.get("expiry_lte")
        return [c for c in contracts
                if (lo is None or c.expiration_date >= lo)
                and (hi is None or c.expiration_date <= hi)]
    return SimpleNamespace(list_option_contracts=list_option_contracts)


# ── the parser records WHOSE date it is ──────────────────────────────────────

def _signal(text, posted_at=THURSDAY):
    result = parse_message(ParsedMessage(content=text, posted_at=posted_at))
    assert result.signals, f"{text!r} did not parse"
    return result.signals[0]


def test_an_undated_entry_is_flagged_as_our_assumption():
    sig = _signal("NVDA 215P @here @Sniper 1.4 risky")
    assert sig.expiration == THURSDAY.date()
    assert sig.expiry_assumed is True


def test_a_stated_date_is_not_flagged():
    sig = _signal("AAPL $350 Call 10/23/26 @0.40")
    assert sig.expiration == FRIDAY
    assert sig.expiry_assumed is False


def test_an_explicit_0dte_is_the_traders_instruction_not_ours():
    # "0DTE" is the trader naming today; it must not be shiftable.
    sig = _signal("SPY 763C 0DTE @ .55")
    assert sig.expiration == THURSDAY.date()
    assert sig.expiry_assumed is False


# ── execution shifts only what it assumed ────────────────────────────────────

def test_assumed_expiry_shifts_to_the_next_listed_one():
    adapter = _chain(_contract("215", "put", FRIDAY))
    shifted = ex._check_contract_exists(
        adapter, "NVDA", Decimal("215"), OptionRight.PUT, THURSDAY.date(),
        expiry_assumed=True,
    )
    assert shifted == FRIDAY


def test_a_stated_expiry_is_refused_not_shifted():
    adapter = _chain(_contract("215", "put", FRIDAY))
    with pytest.raises(ex.ExecutionRefused) as exc:
        ex._check_contract_exists(
            adapter, "NVDA", Decimal("215"), OptionRight.PUT, THURSDAY.date(),
        )
    assert "no options expiring" in str(exc.value)


def test_the_shift_never_moves_backwards():
    # An earlier expiry exists in the window; taking it would buy LESS time
    # than the alert implied.
    adapter = _chain(_contract("215", "put", date(2026, 10, 16)),
                     _contract("215", "put", FRIDAY))
    shifted = ex._check_contract_exists(
        adapter, "NVDA", Decimal("215"), OptionRight.PUT, THURSDAY.date(),
        expiry_assumed=True,
    )
    assert shifted == FRIDAY


def test_a_listed_contract_is_left_alone():
    adapter = _chain(_contract("215", "put", THURSDAY.date()))
    assert ex._check_contract_exists(
        adapter, "NVDA", Decimal("215"), OptionRight.PUT, THURSDAY.date(),
        expiry_assumed=True,
    ) is None


def test_no_shift_when_the_chain_lists_nothing_nearby():
    adapter = _chain()
    with pytest.raises(ex.ExecutionRefused):
        ex._check_contract_exists(
            adapter, "NVDA", Decimal("215"), OptionRight.PUT, THURSDAY.date(),
            expiry_assumed=True,
        )


def test_shifting_does_not_excuse_a_strike_the_chain_lacks():
    # The shift fixes the DATE. A strike that doesn't exist on the new date is
    # still refused — by resolve()'s re-check, mirrored here.
    adapter = _chain(_contract("250", "put", FRIDAY))
    shifted = ex._check_contract_exists(
        adapter, "NVDA", Decimal("215"), OptionRight.PUT, THURSDAY.date(),
        expiry_assumed=True,
    )
    assert shifted == FRIDAY
    with pytest.raises(ex.ExecutionRefused):
        ex._check_contract_exists(
            adapter, "NVDA", Decimal("215"), OptionRight.PUT, shifted,
        )


def test_an_adapter_that_cannot_list_contracts_is_not_blocked():
    assert ex._check_contract_exists(
        SimpleNamespace(), "NVDA", Decimal("215"), OptionRight.PUT,
        THURSDAY.date(), expiry_assumed=True,
    ) is None
