"""Tests for turning an approved Discord alert into a broker order.

Almost every test here asserts a REFUSAL. That's the point: parsing decides what
a message said, this layer decides whether there's enough certainty to put real
money behind it. The asymmetry drives the design — skipping a real alert costs a
missed trade, guessing costs a real position in the wrong contract.
"""
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_execution as ex
from app.models.order import OptionRight

TODAY = datetime.now(timezone.utc).date()
FUTURE = TODAY + timedelta(days=7)


class _Pos:
    def __init__(self, symbol="MSFT", strike="100", right=OptionRight.CALL,
                 expiry=None, qty="5"):
        self.symbol = symbol
        self.option_strike = Decimal(strike)
        self.option_right = right
        self.option_expiry = expiry or FUTURE
        self.quantity = Decimal(qty)


class _Adapter:
    def __init__(self, positions=None, quote=None, raises=False):
        self._positions = positions or []
        self._quote = quote
        self._raises = raises

    def get_positions(self):
        if self._raises:
            raise RuntimeError("broker unreachable")
        return self._positions

    def get_option_quote(self, occ):
        return self._quote


class _Acct:
    def __init__(self):
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.connection_status = "connected"
        self.encrypted_credentials = "x"


class _User:
    def __init__(self):
        self.id = uuid.uuid4()


def _wire(monkeypatch, adapter, accounts=None):
    """Point the module at fake broker plumbing."""
    acct = _Acct()
    monkeypatch.setattr(ex, "adapter_for", lambda a, c: adapter)
    monkeypatch.setattr(ex, "decrypt_json", lambda c: {})
    monkeypatch.setattr(ex, "_broker_account", lambda db, user: (accounts or acct))
    return acct


def _signal(**over):
    base = {
        "action": "BUY", "asset_type": "OPTION", "symbol": "MSFT",
        "strike": "100", "option_type": "CALL", "expiration": FUTURE.isoformat(),
        "quantity": "1", "order_type": "LIMIT", "limit_price": "1.90",
    }
    base.update(over)
    return base


# ── the happy path ───────────────────────────────────────────────────────────

def test_a_complete_buy_resolves_to_an_order(monkeypatch):
    _wire(monkeypatch, _Adapter())
    r = ex.resolve(None, _User(), _signal())
    assert r.payload.side.value == "buy"
    assert r.payload.symbol == "MSFT"
    assert r.payload.quantity == Decimal("1")
    assert r.payload.limit_price == Decimal("1.90")
    assert r.is_closing is False


def test_entries_are_limit_orders(monkeypatch):
    _wire(monkeypatch, _Adapter())
    assert ex.resolve(None, _User(), _signal()).payload.order_type.value == "limit"


def test_closes_are_market_orders(monkeypatch):
    """An unfilled exit is worse than a slightly worse fill — a limit sell can
    sit while the position moves against you."""
    _wire(monkeypatch, _Adapter(positions=[_Pos()]))
    r = ex.resolve(None, _User(), _signal(action="SELL", limit_price="2.50"))
    assert r.payload.order_type.value == "market"
    assert r.payload.limit_price is None


def test_a_close_needs_no_quote(monkeypatch):
    """Market closes removed a failure mode: an exit used to be refused when no
    live quote was available."""
    _wire(monkeypatch, _Adapter(positions=[_Pos()], quote=None))
    r = ex.resolve(None, _User(), _signal(action="SELL", limit_price=None))
    assert r.payload.order_type.value == "market"


# ── close intent: the SELL_TO_OPEN trap ──────────────────────────────────────

def test_a_sell_is_marked_as_a_close(monkeypatch):
    """An alert-channel SELL is always an exit. Without this the order goes to
    the broker as SELL_TO_OPEN — rejected at best, a naked short at worst."""
    _wire(monkeypatch, _Adapter(positions=[_Pos()]))
    r = ex.resolve(None, _User(), _signal(action="SELL", limit_price="2.50"))
    assert r.is_closing is True


def test_a_close_sizes_from_the_position_not_the_alert(monkeypatch):
    """The author's size is theirs. Selling fewer than you hold strands the
    remainder; selling more is rejected or opens a short."""
    _wire(monkeypatch, _Adapter(positions=[_Pos(qty="5")]))
    r = ex.resolve(None, _User(), _signal(action="SELL", quantity="1", limit_price="2.50"))
    assert r.payload.quantity == Decimal("5")
    assert "quantity" in r.resolutions


def test_closing_something_you_dont_hold_is_refused(monkeypatch):
    _wire(monkeypatch, _Adapter(positions=[]))
    with pytest.raises(ex.ExecutionRefused, match="no matching position|hold no position"):
        ex.resolve(None, _User(), _signal(action="SELL", limit_price="2.50"))


# ── resolving an incomplete contract ─────────────────────────────────────────

def test_a_missing_expiry_is_taken_from_the_open_position(monkeypatch):
    """"✂️ $MSFT 100c" names no expiry — the held contract supplies it."""
    _wire(monkeypatch, _Adapter(positions=[_Pos(expiry=FUTURE)]))
    r = ex.resolve(None, _User(), _signal(action="SELL", expiration=None, limit_price="2.50"))
    assert r.payload.option_expiry == FUTURE
    assert "expiration" in r.resolutions


def test_a_symbol_only_close_resolves_the_whole_contract(monkeypatch):
    """"META -> 100%" names nothing but the ticker."""
    _wire(monkeypatch, _Adapter(positions=[_Pos(symbol="META", strike="300")]))
    r = ex.resolve(None, _User(), _signal(
        action="SELL", symbol="META", strike=None, option_type=None,
        expiration=None, limit_price="2.50",
    ))
    assert r.payload.option_strike == Decimal("300")


def test_an_ambiguous_close_is_refused(monkeypatch):
    """Two open contracts fit the alert. Picking one is a coin flip on which
    position to trade."""
    _wire(monkeypatch, _Adapter(positions=[
        _Pos(symbol="META", strike="300"), _Pos(symbol="META", strike="310"),
    ]))
    with pytest.raises(ex.ExecutionRefused, match="doesn't say which"):
        ex.resolve(None, _User(), _signal(
            action="SELL", symbol="META", strike=None, option_type=None,
            expiration=None, limit_price="2.50",
        ))


def test_an_unresolvable_contract_is_refused(monkeypatch):
    _wire(monkeypatch, _Adapter(positions=[]))
    with pytest.raises(ex.ExecutionRefused, match="no matching position"):
        ex.resolve(None, _User(), _signal(
            action="SELL", strike=None, option_type=None, expiration=None, limit_price="2.50",
        ))


# ── expiry ───────────────────────────────────────────────────────────────────

def test_an_expired_contract_is_refused(monkeypatch):
    """The parser deliberately keeps a recently-past date rather than rolling it
    a year forward — this is the check that catches it."""
    _wire(monkeypatch, _Adapter())
    past = (TODAY - timedelta(days=3)).isoformat()
    with pytest.raises(ex.ExecutionRefused, match="expired"):
        ex.resolve(None, _User(), _signal(expiration=past))


def test_todays_expiry_is_allowed(monkeypatch):
    """0DTE is the whole point of these channels."""
    _wire(monkeypatch, _Adapter())
    r = ex.resolve(None, _User(), _signal(expiration=TODAY.isoformat()))
    assert r.payload.option_expiry == TODAY


# ── pricing ──────────────────────────────────────────────────────────────────

def test_a_missing_price_comes_from_the_live_quote(monkeypatch):
    _wire(monkeypatch, _Adapter(quote={"bid": "2.00", "ask": "2.20"}))
    r = ex.resolve(None, _User(), _signal(limit_price=None))
    # A BUY prices through the ask so the limit still fills.
    assert r.payload.limit_price == Decimal("2.20")
    assert "limit_price" in r.resolutions


def test_a_close_carries_no_limit_price_at_all(monkeypatch):
    """Closes are market orders, so no price is derived even when a quote
    exists."""
    _wire(monkeypatch, _Adapter(positions=[_Pos()], quote={"bid": "2.00", "ask": "2.20"}))
    r = ex.resolve(None, _User(), _signal(action="SELL", limit_price=None))
    assert r.payload.limit_price is None


def test_no_price_and_no_quote_is_refused(monkeypatch):
    """Inventing a limit would be guessing the level to trade at."""
    _wire(monkeypatch, _Adapter(quote=None))
    with pytest.raises(ex.ExecutionRefused, match="no live quote"):
        ex.resolve(None, _User(), _signal(limit_price=None))


# ── broker plumbing ──────────────────────────────────────────────────────────

def test_a_broker_read_failure_is_refused_not_treated_as_flat(monkeypatch):
    """A failed position read must never look like "no position" — that would
    turn a close into an opening short."""
    _wire(monkeypatch, _Adapter(positions=[], raises=True))
    with pytest.raises(ex.ExecutionRefused, match="Couldn't read your positions"):
        ex.resolve(None, _User(), _signal(action="SELL", limit_price="2.50"))


def test_an_alert_with_no_quantity_is_refused(monkeypatch):
    _wire(monkeypatch, _Adapter())
    with pytest.raises(ex.ExecutionRefused, match="no quantity"):
        ex.resolve(None, _User(), _signal(quantity=None))


# ── idempotency ──────────────────────────────────────────────────────────────

class _Msg:
    def __init__(self, order_id=None, status=None):
        self.order_id = order_id
        self.status = status
        self.status_reason = None


def test_an_alert_that_already_placed_an_order_is_never_replaced():
    """The decision endpoint and the auto path can both reach an approved alert.
    One alert, one order."""
    from app.models.discord_message import DiscordMessageStatus

    assert ex.already_executed(_Msg(order_id=uuid.uuid4())) is True
    assert ex.already_executed(_Msg(status=DiscordMessageStatus.ORDER_CREATED)) is True
    assert ex.already_executed(_Msg()) is False


# ── The contract must actually exist ─────────────────────────────────────────
# An alert can name a date no option expires on. Alpaca answers "asset not
# found", which tells the trader nothing — these turn that into a specific
# message BEFORE an order is sent.

class _Contract:
    def __init__(self, strike, cp="call", expiry=None):
        self.strike_price = str(strike)
        self.type = cp
        self.expiration_date = expiry or FUTURE


class _ChainAdapter(_Adapter):
    def __init__(self, contracts=None, by_window=None, **kw):
        super().__init__(**kw)
        self._contracts = contracts
        self._by_window = by_window or {}

    def list_option_contracts(self, underlying, expiry_gte, expiry_lte, limit=0):
        if expiry_gte != expiry_lte:                 # the "nearby expiries" probe
            return self._by_window.get("near", [])
        return self._contracts if self._contracts is not None else []


def test_an_expiry_with_no_contracts_is_refused_before_placing(monkeypatch):
    """"10/10" was a Saturday — nothing expires then."""
    _wire(monkeypatch, _ChainAdapter(contracts=[]))
    with pytest.raises(ex.ExecutionRefused, match="no options expiring"):
        ex.resolve(None, _User(), _signal())


def test_the_refusal_names_the_weekday_and_suggests_real_expiries(monkeypatch):
    near = [_Contract(100, expiry=FUTURE), _Contract(100, expiry=FUTURE + timedelta(days=7))]
    _wire(monkeypatch, _ChainAdapter(contracts=[], by_window={"near": near}))
    with pytest.raises(ex.ExecutionRefused) as exc:
        ex.resolve(None, _User(), _signal())
    msg = str(exc.value)
    assert FUTURE.strftime("%A") in msg          # the weekday asked for
    assert "Nearest expiries" in msg


def test_a_strike_that_doesnt_exist_is_refused(monkeypatch):
    _wire(monkeypatch, _ChainAdapter(contracts=[_Contract(105), _Contract(110)]))
    with pytest.raises(ex.ExecutionRefused, match="has no \\$100 call"):
        ex.resolve(None, _User(), _signal())


def test_a_real_contract_passes_the_chain_check(monkeypatch):
    _wire(monkeypatch, _ChainAdapter(contracts=[_Contract(100), _Contract(105)]))
    assert ex.resolve(None, _User(), _signal()).payload.option_strike == Decimal("100")


def test_a_call_alert_is_not_satisfied_by_a_put_at_the_same_strike(monkeypatch):
    _wire(monkeypatch, _ChainAdapter(contracts=[_Contract(100, cp="put")]))
    with pytest.raises(ex.ExecutionRefused, match="has no \\$100 call"):
        ex.resolve(None, _User(), _signal())


def test_a_chain_lookup_failure_does_not_block_the_order(monkeypatch):
    """The broker still validates on its side. A failed chain read is not
    evidence the contract is bad, so it must not refuse a valid order."""
    class _Broken(_Adapter):
        def list_option_contracts(self, **kw):
            raise RuntimeError("chain endpoint down")

    _wire(monkeypatch, _Broken())
    assert ex.resolve(None, _User(), _signal()).payload.symbol == "MSFT"


def test_an_adapter_without_chain_support_is_skipped(monkeypatch):
    """Only Alpaca implements the chain today; other brokers must still work."""
    _wire(monkeypatch, _Adapter())          # no list_option_contracts
    assert ex.resolve(None, _User(), _signal()).payload.symbol == "MSFT"


# ── Sizing: multiplier and the per-order dollar ceiling ──────────────────────

def test_the_multiplier_scales_an_entry(monkeypatch):
    _wire(monkeypatch, _ChainAdapter(contracts=[_Contract(100)]))
    r = ex.resolve(None, _User(), _signal(quantity="1"), ex.Sizing(multiplier=3))
    assert r.payload.quantity == Decimal("3")
    assert r.resolutions["quantity"] == "3 (1 x 3 multiplier)"


def test_the_multiplier_never_scales_a_close(monkeypatch):
    """An exit sells the position actually held. Multiplying it would either
    strand size or try to sell more than exists."""
    _wire(monkeypatch, _ChainAdapter(contracts=[_Contract(100)], positions=[_Pos(qty="5")]))
    r = ex.resolve(
        None, _User(), _signal(action="SELL", quantity="1", limit_price="2.50"),
        ex.Sizing(multiplier=10),
    )
    assert r.payload.quantity == Decimal("5")       # the position, not 1 x 10


def test_a_multiplier_of_one_is_the_default(monkeypatch):
    _wire(monkeypatch, _ChainAdapter(contracts=[_Contract(100)]))
    assert ex.resolve(None, _User(), _signal()).payload.quantity == Decimal("1")


def test_an_affordable_contract_is_not_resized(monkeypatch):
    """The limit is a judgement on contract PRICE, not a budget to spend down —
    so an affordable contract goes through at full size."""
    _wire(monkeypatch, _ChainAdapter(contracts=[_Contract(100)]))
    r = ex.resolve(
        None, _User(), _signal(quantity="5"),      # $1.90 x 100 = $190 each
        ex.Sizing(max_per_contract=Decimal("500")),
    )
    assert r.payload.quantity == Decimal("5")


def test_an_expensive_contract_skips_the_entry(monkeypatch):
    """$9.00 x 100 = $900, above a $500 ceiling. Matches
    SubscriberSettings.max_per_contract: skip the entry, don't trim it."""
    _wire(monkeypatch, _ChainAdapter(contracts=[_Contract(100)]))
    with pytest.raises(ex.ExecutionRefused, match="max per contract"):
        ex.resolve(
            None, _User(), _signal(limit_price="9.00"),
            ex.Sizing(max_per_contract=Decimal("500")),
        )


def test_the_ceiling_is_per_contract_not_per_order(monkeypatch):
    """10 contracts at $190 each is $1,900 of order value, but each CONTRACT is
    under the $500 ceiling — so it goes through untouched."""
    _wire(monkeypatch, _ChainAdapter(contracts=[_Contract(100)]))
    r = ex.resolve(
        None, _User(), _signal(quantity="1"),
        ex.Sizing(multiplier=10, max_per_contract=Decimal("500")),
    )
    assert r.payload.quantity == Decimal("10")


def test_no_ceiling_means_no_limit(monkeypatch):
    _wire(monkeypatch, _ChainAdapter(contracts=[_Contract(100)]))
    r = ex.resolve(None, _User(), _signal(quantity="7"), ex.Sizing(max_per_contract=None))
    assert r.payload.quantity == Decimal("7")


def test_the_ceiling_never_blocks_a_close(monkeypatch):
    """You must be able to exit a position you already hold, whatever it costs —
    a ceiling that blocked closes would trap the trader in a trade."""
    _wire(monkeypatch, _ChainAdapter(contracts=[_Contract(100)], positions=[_Pos(qty="9")]))
    r = ex.resolve(
        None, _User(), _signal(action="SELL", limit_price="9.00"),
        ex.Sizing(max_per_contract=Decimal("100")),
    )
    assert r.payload.quantity == Decimal("9")


def test_the_ceiling_does_not_apply_to_stock(monkeypatch):
    """Options only, matching SubscriberSettings.max_per_contract — a share
    price isn't a contract value."""
    _wire(monkeypatch, _Adapter())
    r = ex.resolve(
        None, _User(),
        _signal(asset_type="STOCK", strike=None, option_type=None,
                expiration=None, limit_price="250"),
        ex.Sizing(max_per_contract=Decimal("100")),
    )
    assert r.payload.quantity == Decimal("1")


def test_contract_type_is_read_from_the_enum_value_not_its_repr():
    """Alpaca's ContractType stringifies as "ContractType.PUT", so reading the
    first character of str() made every contract look like a call — puts were
    rejected outright and calls skipped the call/put check."""
    class _Enum:
        value = "put"

        def __str__(self):
            return "ContractType.PUT"

    class _C:
        type = _Enum()

    assert ex._contract_type(_C()) == "P"


def test_contract_type_still_works_for_plain_strings():
    class _C:
        type = "call"

    assert ex._contract_type(_C()) == "C"


def test_a_put_alert_matches_a_put_contract(monkeypatch):
    """The end-to-end case that was failing: "$SPY 759 PUT" against a chain that
    contains that put."""
    class _PutEnum:
        value = "put"

        def __str__(self):
            return "ContractType.PUT"

    class _PutContract:
        strike_price = "759"
        type = _PutEnum()
        expiration_date = FUTURE

    _wire(monkeypatch, _ChainAdapter(contracts=[_PutContract()]))
    r = ex.resolve(None, _User(), _signal(strike="759", option_type="PUT"))
    assert r.payload.option_strike == Decimal("759")
    assert r.payload.option_right.value == "put"


def test_a_call_alert_is_still_rejected_when_only_puts_exist(monkeypatch):
    """The mirror of the bug: calls must no longer match puts."""
    class _PutEnum:
        value = "put"

    class _PutContract:
        strike_price = "100"
        type = _PutEnum()
        expiration_date = FUTURE

    _wire(monkeypatch, _ChainAdapter(contracts=[_PutContract()]))
    with pytest.raises(ex.ExecutionRefused, match="has no \\$100 call"):
        ex.resolve(None, _User(), _signal())


# ── index options are filed under a different root than they trade under ─────

def test_an_index_weekly_looks_up_the_chain_under_its_index_root(monkeypatch):
    """SPXW260916C07585000 is listed in SPX's chain, not SPXW's. Asking for the
    SPXW chain returns nothing, which reads as "no such contract" when only the
    lookup key was wrong — a real alert was rejected this way."""
    asked = {}

    class _Chain(_Adapter):
        def list_option_contracts(self, underlying=None, **kw):
            asked["underlying"] = underlying
            if underlying != "SPX":
                return []
            c = type("C", (), {"strike_price": Decimal("7585"),
                               "type": type("T", (), {"value": "call"})()})()
            return [c]

    _wire(monkeypatch, _Chain())
    r = ex.resolve(None, _User(), _signal(
        symbol="SPXW", strike="7585", limit_price="39.47",
        expiration=FUTURE.isoformat()))

    assert asked["underlying"] == "SPX"          # looked up under the index root
    assert r.payload.symbol == "SPXW"            # but trades under its own


def test_an_ordinary_symbol_keeps_its_own_chain_root(monkeypatch):
    asked = {}

    class _Chain(_Adapter):
        def list_option_contracts(self, underlying=None, **kw):
            asked["underlying"] = underlying
            c = type("C", (), {"strike_price": Decimal("100"),
                               "type": type("T", (), {"value": "call"})()})()
            return [c]

    _wire(monkeypatch, _Chain())
    ex.resolve(None, _User(), _signal())
    assert asked["underlying"] == "MSFT"


def test_the_chain_root_map_covers_the_common_index_weeklies():
    assert ex._chain_root("SPXW") == "SPX"
    assert ex._chain_root("NDXP") == "NDX"
    assert ex._chain_root("RUTW") == "RUT"
    assert ex._chain_root("spxw") == "SPX"       # case-insensitive
    assert ex._chain_root("AAPL") == "AAPL"      # untouched
