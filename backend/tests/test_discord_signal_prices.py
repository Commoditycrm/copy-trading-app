"""The contract a Discord alert row is priced by (services/discord_signal_prices.py).

QA 2026-10-22: alerts awaiting approval showed "—" for Current price whenever
the alert didn't name a whole contract — "✂️ $SPY 770c +43%" (no expiry),
"SL hit sir 50%" (nothing). Only rows posted within the hour and not yet
submitted or accepted are priced.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.services import discord_signal_prices as sp

NOW = datetime(2026, 10, 22, 14, 0, tzinfo=timezone.utc)
SRC = uuid.uuid4()


def _msg(minutes_ago=5, status="parsed", decision="pending"):
    return SimpleNamespace(posted_at=NOW - timedelta(minutes=minutes_ago), created_at=None,
                           status=status, decision=decision, source_id=SRC)


def _guard(symbol="SPY", strike="770", right="call", expiry=date(2026, 10, 22)):
    return SimpleNamespace(symbol=symbol, option_strike=D(strike), option_right=right, option_expiry=expiry)


@pytest.fixture
def opens(monkeypatch):
    held: list = []
    monkeypatch.setattr(sp, "_open_positions", lambda db, uid, source_id: held)
    return held


def _live(sig, m=None):
    return sp.live_symbol(None, uuid.uuid4(), m or _msg(), sig, cache={}, now=NOW)


def test_a_whole_contract_is_priced_as_named(opens):
    sig = {"asset_type": "OPTION", "symbol": "SKHY", "strike": "165", "option_type": "CALL",
           "expiration": "2026-10-30"}
    assert _live(sig) == "SKHY261030C00165000"


def test_no_expiry_takes_the_channels_open_position(opens):
    opens.append(_guard())
    sig = {"asset_type": "OPTION", "symbol": "SPY", "strike": "770", "option_type": "CALL",
           "expiration": None, "expiry_unspecified": True}
    assert _live(sig) == "SPY261022C00770000"


def test_an_exit_that_names_nothing_takes_the_channels_newest_position(opens):
    opens.extend([_guard("QQQ", "726", "call", date(2026, 10, 22)), _guard()])
    sig = {"asset_type": "OPTION", "action": "SELL", "symbol": None, "contract_unspecified": True}
    assert _live(sig) == "QQQ261022C00726000"


def test_a_named_symbol_only_matches_its_own_position(opens):
    opens.extend([_guard("QQQ", "726", "call"), _guard("SPY", "770", "put")])
    sig = {"asset_type": "OPTION", "symbol": "SPY", "option_type": "PUT", "contract_unspecified": True}
    assert _live(sig) == "SPY261022P00770000"


def test_an_entry_with_no_expiry_and_nothing_held_takes_the_nearest_listed(opens, monkeypatch):
    monkeypatch.setattr(sp, "_nearest_expiry", lambda s, k, r, cache: date(2026, 10, 23))
    sig = {"asset_type": "OPTION", "symbol": "AMZN", "strike": "245", "option_type": "PUT"}
    assert _live(sig) == "AMZN261023P00245000"


def test_nothing_to_go_on_is_not_priced(opens):
    assert _live({"asset_type": "OPTION", "action": "SELL"}) is None


def test_a_stock_is_priced_by_its_ticker(opens):
    assert _live({"asset_type": "STOCK", "symbol": "nvda"}) == "NVDA"


@pytest.mark.parametrize("m, priced", [
    (_msg(minutes_ago=59), True),
    (_msg(minutes_ago=61), False),                                     # older than the hour
    (_msg(status="order_created", decision="approved"), False),        # submitted
    (_msg(decision="approved"), False),                                # accepted
    (_msg(decision="rejected"), True),                                 # rejected: not submitted or accepted
    (_msg(status="order_failed", decision="approved"), True),          # failed reads as rejected
])
def test_only_the_last_hour_and_never_submitted_or_accepted(opens, m, priced):
    sig = {"asset_type": "OPTION", "symbol": "SKHY", "strike": "165", "option_type": "CALL",
           "expiration": "2026-10-30"}
    assert (_live(sig, m) is not None) is priced


def test_the_nearest_expiry_is_looked_up_once_per_contract(monkeypatch):
    calls = []
    from app.services import discord_execution as ex

    monkeypatch.setattr(ex, "_data_account_expiries",
                        lambda root, strike, right, start, end: calls.append(root) or [date(2026, 10, 23)])
    cache: dict = {}
    for _ in range(3):
        assert sp._nearest_expiry("AMZN", D("245"), "put", cache) == date(2026, 10, 23)
    assert calls == ["AMZN"]
