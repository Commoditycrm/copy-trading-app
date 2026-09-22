"""Unit tests for the direct-Webull execution adapter (subscriber mirrors).

Covers the write path added so SUBSCRIBERS can execute mirror orders on a direct
Webull account (not only via SnapTrade): the Webull order-dict construction, the
open-vs-close (position_intent) mapping — the load-bearing SELL_TO_CLOSE fix — and
the get_order_detail response parsing. All offline: no SDK/network calls; the
order builders are pure, and the detail-parse path is driven by a fake SDK client.

Standalone:  .venv/bin/python tests/test_webull_adapter_orders.py
Or pytest:   pytest tests/test_webull_adapter_orders.py
"""
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brokers.base import BrokerOrderRequest
from app.brokers.webull import WebullAdapter
from app.models.order import (
    InstrumentType,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
)


def _adapter() -> WebullAdapter:
    return WebullAdapter(
        {"app_key": "k", "app_secret": "s", "account_id": "ACC1", "region_id": "us"}
    )


# ── client_order_id ──────────────────────────────────────────────────────────
def test_client_order_id_is_stable_32_char_from_uuid():
    a = _adapter()
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity=Decimal("1"),
        client_order_id="11112222-3333-4444-5555-666677778888",
    )
    coid = a._client_order_id(req)
    assert coid == "11112222333344445555666677778888"  # dashes stripped
    assert len(coid) == 32                              # Webull's max
    assert a._client_order_id(req) == coid              # deterministic → idempotent


# ── stock orders ─────────────────────────────────────────────────────────────
def test_stock_market_buy_dict():
    a = _adapter()
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="aapl", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity=Decimal("3"),
    )
    d = a._build_stock_order(req, "c1")
    assert d["symbol"] == "AAPL" and d["market"] == "US"
    assert d["side"] == "BUY" and d["order_type"] == "MARKET"
    assert d["quantity"] == "3" and d["time_in_force"] == "DAY"
    assert d["support_trading_session"] == "CORE"   # market can't be extended
    assert "limit_price" not in d


def test_stock_limit_sell_extended_hours_uses_all_session():
    a = _adapter()
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=Decimal("10"),
        limit_price=Decimal("185.5"), extended_hours=True,
    )
    d = a._build_stock_order(req, "c2")
    assert d["side"] == "SELL" and d["order_type"] == "LIMIT"
    assert d["limit_price"] == "185.50"                # 2dp for >= $1
    assert d["support_trading_session"] == "ALL"       # extended-hours limit


# ── option orders + the open/close (position_intent) mapping ─────────────────
def _opt(side, is_closing, right=OptionRight.CALL, otype=OrderType.LIMIT):
    return BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION, symbol="AAPL", side=side,
        order_type=otype, quantity=Decimal("1"),
        limit_price=Decimal("0.45") if otype == OrderType.LIMIT else None,
        option_expiry=date(2026, 6, 19), option_strike=Decimal("220"),
        option_right=right, is_closing=is_closing,
    )


def test_option_open_intents():
    a = _adapter()
    assert a._position_intent(_opt(OrderSide.BUY, False)) == "BUY_TO_OPEN"
    assert a._position_intent(_opt(OrderSide.SELL, False)) == "SELL_TO_OPEN"


def test_option_close_intents_sell_to_close():
    """The regression that motivated this: a closing SELL must be SELL_TO_CLOSE,
    never SELL_TO_OPEN (Webull rejects the latter 'no position to close')."""
    a = _adapter()
    assert a._position_intent(_opt(OrderSide.SELL, True)) == "SELL_TO_CLOSE"
    assert a._position_intent(_opt(OrderSide.BUY, True)) == "BUY_TO_CLOSE"


def test_option_order_dict_carries_us_option_leg():
    a = _adapter()
    d = a._build_option_order(_opt(OrderSide.SELL, True, right=OptionRight.PUT,
                                   otype=OrderType.MARKET), "c4")
    assert d["position_intent"] == "SELL_TO_CLOSE" and d["side"] == "SELL"
    assert d["order_type"] == "MARKET" and d["option_strategy"] == "SINGLE"
    assert len(d["legs"]) == 1
    leg = d["legs"][0]
    # Category header derives from these — must be an OPTION/US leg.
    assert leg["instrument_type"] == "OPTION" and leg["market"] == "US"
    assert leg["option_type"] == "PUT" and leg["strike_price"] == "220.00"
    assert leg["option_expire_date"] == "2026-06-19"
    assert leg["position_intent"] == "SELL_TO_CLOSE"


def test_option_missing_terms_raises():
    a = _adapter()
    bad = BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION, symbol="AAPL", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity=Decimal("1"),
    )
    try:
        a._build_option_order(bad, "c")
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError for option order without terms")


# ── formatting ───────────────────────────────────────────────────────────────
def test_price_precision_and_qty():
    a = _adapter()
    assert a._fmt_price(Decimal("0.455")) == "0.4550"   # 4dp for < $1
    assert a._fmt_price(Decimal("185.505")) == "185.51"  # 2dp for >= $1
    assert a._fmt_qty(Decimal("3.0")) == "3"             # whole-share tidy


# ── detail parsing (get_order / cancel status) via a fake SDK client ─────────
class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeOrderOps:
    def __init__(self, detail_resp):
        self._detail = detail_resp

    def get_order_detail(self, account_id, client_order_id):
        return self._detail


class _FakeTrade:
    def __init__(self, detail_resp):
        self.order_v3 = _FakeOrderOps(detail_resp)


def test_fetch_detail_parses_filled_option_leg():
    a = _adapter()
    body = {
        "order_id": "WB123", "client_order_id": "c9", "category": "US_OPTION",
        "items": [{
            "order_status": "FILLED", "filled_qty": "2", "filled_price": "0.51",
            # Webull reports execution time as epoch millis. Order History used
            # to ignore it and show our detection time instead.
            "filled_time": 1789741805000,
        }],
    }
    parsed = a._fetch_detail(_FakeTrade(_FakeResp(200, body)), "c9")
    assert parsed is not None
    _order, is_option, status, filled_qty, filled_px, filled_at = parsed
    assert is_option is True
    assert status == OrderStatus.FILLED
    assert filled_qty == Decimal("2") and filled_px == Decimal("0.51")
    assert filled_at == datetime(2026, 9, 18, 14, 30, 5, tzinfo=timezone.utc)


def test_fetch_detail_partial_stock():
    a = _adapter()
    body = {"category": "US_STOCK",
            "items": [{"order_status": "PARTIAL_FILLED", "filled_qty": "1", "filled_price": "10"}]}
    _o, is_option, status, q, p, _t = a._fetch_detail(_FakeTrade(_FakeResp(200, body)), "c")
    assert is_option is False and status == OrderStatus.PARTIALLY_FILLED
    assert q == Decimal("1") and p == Decimal("10")


def test_fetch_detail_none_on_non_200():
    a = _adapter()
    assert a._fetch_detail(_FakeTrade(_FakeResp(500, None)), "c") is None


def test_get_order_maps_status():
    a = _adapter()
    body = {"category": "US_STOCK", "items": [{"order_status": "SUBMITTED", "filled_qty": "0"}]}

    class _T:
        order_v3 = _FakeOrderOps(_FakeResp(200, body))

    # monkeypatch the cached client so get_order uses our fake trade
    a._trade_client = lambda: _T()  # type: ignore[method-assign]
    res = a.get_order("c")
    assert res.status == OrderStatus.SUBMITTED
    assert res.broker_order_id == "c" and res.filled_quantity == Decimal("0")


# ── market data (drives marketable-limit pricing) ───────────────────────────
# Webull gates quotes behind a SEPARATE entitlement from trading, so these
# methods are best-effort by contract: every caller treats None as "leave the
# order alone" and falls back to trader-anchored pricing. What must hold is that
# a GOOD response is parsed, a BAD one degrades quietly, and an ENTITLEMENT
# failure stops us spending a doomed HTTP call per mirror on every fanout.
import app.brokers.webull as wb  # noqa: E402


def _reset_quote_backoff():
    with wb._quotes_disabled_lock:
        wb._quotes_disabled_until.clear()


class _FakeMarketData:
    def __init__(self, resp, raises=None):
        self._resp, self._raises = resp, raises
        self.calls = 0

    def get_snapshot(self, symbols, category, **kw):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._resp

    def get_option_snapshot(self, symbols, category):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._resp


class _FakeDataClient:
    def __init__(self, resp, raises=None):
        md = _FakeMarketData(resp, raises)
        self.market_data = md
        self.option_market_data = md


def _quote_adapter(resp, raises=None):
    _reset_quote_backoff()
    a = _adapter()
    a._data_client = lambda: _FakeDataClient(resp, raises)  # type: ignore[method-assign]
    return a


def test_stock_latest_price_parses_snapshot():
    a = _quote_adapter(_FakeResp(200, {"data": [{"symbol": "AAPL", "last_price": "187.42"}]}))
    assert a.get_stock_latest_price("aapl") == Decimal("187.42")


def test_stock_latest_price_accepts_a_bare_list_body():
    a = _quote_adapter(_FakeResp(200, [{"symbol": "AAPL", "close": "12.5"}]))
    assert a.get_stock_latest_price("AAPL") == Decimal("12.5")


def test_stock_latest_price_none_on_non_200():
    assert _quote_adapter(_FakeResp(500, None)).get_stock_latest_price("AAPL") is None


def test_option_quote_parses_flat_bid_ask():
    a = _quote_adapter(_FakeResp(200, {"data": [
        {"symbol": "AAPL260619C00220000", "bid_price": "3.00", "ask_price": "3.20"},
    ]}))
    assert a.get_option_latest_quote("AAPL260619C00220000") == (Decimal("3.00"), Decimal("3.20"))


def test_option_quote_parses_nested_depth_lists():
    """Some shapes return top-of-book inside bid/ask ladders."""
    a = _quote_adapter(_FakeResp(200, {"data": [{
        "bid_list": [{"price": "1.05", "size": "10"}],
        "ask_list": [{"price": "1.15", "size": "8"}],
    }]}))
    assert a.get_option_latest_quote("AAPL260619C00220000") == (Decimal("1.05"), Decimal("1.15"))


def test_option_quote_one_sided_book():
    a = _quote_adapter(_FakeResp(200, {"data": [{"bid_price": "1.05"}]}))
    bid, ask = a.get_option_latest_quote("AAPL260619C00220000")
    assert bid == Decimal("1.05") and ask is None


def test_option_quote_none_pair_when_unavailable():
    assert _quote_adapter(_FakeResp(200, {"data": []})).get_option_latest_quote("X") == (None, None)


def test_entitlement_failure_suppresses_further_quote_calls():
    """An app_key without the Market Data entitlement must not cost one failed
    request per mirror — a 50-subscriber option close would fire 50 of them."""
    _reset_quote_backoff()
    a = _adapter()
    client = _FakeDataClient(None, raises=RuntimeError(
        "HTTP Status: 403, Code: NO_PERMISSION, Msg: market data not subscribed"
    ))
    a._data_client = lambda: client  # type: ignore[method-assign]

    assert a.get_option_latest_quote("AAPL260619C00220000") == (None, None)
    assert client.option_market_data.calls == 1
    # Second and third attempts short-circuit before touching the SDK.
    assert a.get_option_latest_quote("AAPL260619C00220000") == (None, None)
    assert a.get_stock_latest_price("AAPL") is None
    assert client.option_market_data.calls == 1
    _reset_quote_backoff()


def test_transient_failure_does_not_suppress_quotes():
    """A blip must NOT disable quotes — only an entitlement-shaped failure does,
    or one timeout would blind us for the whole backoff window."""
    _reset_quote_backoff()
    a = _adapter()
    client = _FakeDataClient(None, raises=RuntimeError("read timed out"))
    a._data_client = lambda: client  # type: ignore[method-assign]

    assert a.get_stock_latest_price("AAPL") is None
    assert a.get_stock_latest_price("AAPL") is None
    assert client.market_data.calls == 2     # still trying
    _reset_quote_backoff()


def test_quote_failures_never_raise():
    """Callers price orders with these; an exception escaping would fail the
    mirror instead of degrading it."""
    _reset_quote_backoff()
    a = _adapter()
    a._data_client = lambda: (_ for _ in ()).throw(ImportError("no SDK"))  # type: ignore[method-assign]
    assert a.get_stock_latest_price("AAPL") is None
    assert a.get_option_latest_quote("AAPL260619C00220000") == (None, None)
    _reset_quote_backoff()


# ── account listing (the connect-time picker) ───────────────────────────────
# One Webull app_key reaches EVERY account under the login — Cash, Margin, IRA,
# Futures — and which one we trade is decided purely by the account_id in the
# stored credentials. That id is not the account number shown anywhere in the
# Webull app, so the old free-text field asked users to guess: a real-but-wrong
# id verifies cleanly and then every mirror order trades in the wrong account
# with nothing to flag it. list_accounts feeds the picker that replaces it.

class _FakeAccountOps:
    def __init__(self, accounts_resp, balance_resp=None):
        self._accounts = accounts_resp
        self._balance = balance_resp
        self.balance_calls = []

    def get_account_list(self):
        return self._accounts

    def get_account_balance(self, account_id):
        self.balance_calls.append(account_id)
        if isinstance(self._balance, Exception):
            raise self._balance
        return self._balance


class _FakeTradeAccounts:
    def __init__(self, ops):
        self.account_v2 = ops


_ACCOUNTS_BODY = [
    {"account_id": "ACC-CASH", "account_number": "8XX111", "account_type": "CASH"},
    {"account_id": "ACC-MARGIN", "account_number": "8XX222", "account_type": "MARGIN"},
]


def _accounts_adapter(accounts_body, balance_body=None, account_id="ACC-CASH"):
    a = WebullAdapter(
        {"app_key": "k", "app_secret": "s", "account_id": account_id, "region_id": "us"}
    )
    ops = _FakeAccountOps(_FakeResp(200, accounts_body), balance_body)
    a._trade_client = lambda: _FakeTradeAccounts(ops)  # type: ignore[method-assign]
    a._ops = ops  # type: ignore[attr-defined]
    return a


def test_list_accounts_returns_every_account():
    a = _accounts_adapter(_ACCOUNTS_BODY)
    out = a.list_accounts()
    assert [x["account_id"] for x in out] == ["ACC-CASH", "ACC-MARGIN"]
    assert out[0]["account_number"] == "8XX111"
    assert out[1]["account_type"] == "MARGIN"


def test_list_accounts_with_balances_labels_the_funded_one():
    """Equity is the whole point: it is what lets a user tell their funded
    account from an empty Futures or unfunded Cash one."""
    a = _accounts_adapter(
        _ACCOUNTS_BODY,
        _FakeResp(200, {
            "total_net_liquidation_value": "5200.75",
            "total_asset_currency": "USD",
            "account_currency_assets": [{"buying_power": "10400.00"}],
        }),
    )
    out = a.list_accounts(with_balances=True)
    assert out[0]["total_equity"] == Decimal("5200.75")
    assert out[0]["buying_power"] == Decimal("10400.00")
    assert out[0]["currency"] == "USD"


def test_list_accounts_survives_a_failed_balance_read():
    """An account whose balance can't be read is still offered — just without
    figures. Dropping it would hide the very account they meant to pick."""
    a = _accounts_adapter(_ACCOUNTS_BODY, RuntimeError("balance unavailable"))
    out = a.list_accounts(with_balances=True)
    assert len(out) == 2
    assert out[0]["total_equity"] is None


def test_list_accounts_skips_rows_without_an_id():
    a = _accounts_adapter([{"account_number": "no-id"}, *_ACCOUNTS_BODY])
    assert len(a.list_accounts()) == 2


def test_list_accounts_raises_on_a_bad_response():
    a = WebullAdapter({"app_key": "k", "app_secret": "s", "account_id": "x"})
    a._trade_client = lambda: _FakeTradeAccounts(  # type: ignore[method-assign]
        _FakeAccountOps(_FakeResp(401, None))
    )
    try:
        a.list_accounts()
    except RuntimeError:
        return
    raise AssertionError("expected a RuntimeError for a non-200 account list")


# ── verify_connection surfaces the number, not the opaque id ────────────────
def test_verify_connection_reports_the_human_readable_account_number():
    """broker_account_number is display-only, and showing the number the user
    recognises from the Webull app is how they notice a mislink at a glance."""
    a = _accounts_adapter(_ACCOUNTS_BODY, account_id="ACC-MARGIN")
    info = a.verify_connection()
    assert info.broker_account_id == "8XX222"
    assert info.supports_fractional is False


def test_verify_connection_falls_back_to_the_id_when_no_number():
    a = _accounts_adapter([{"account_id": "ACC-ONLY"}], account_id="ACC-ONLY")
    assert a.verify_connection().broker_account_id == "ACC-ONLY"


def test_verify_connection_rejects_an_id_these_keys_cannot_trade():
    a = _accounts_adapter(_ACCOUNTS_BODY, account_id="ACC-NOPE")
    try:
        a.verify_connection()
    except RuntimeError as exc:
        assert "ACC-NOPE" in str(exc)
        assert "ACC-CASH" in str(exc)   # the message lists what IS available
        return
    raise AssertionError("expected a RuntimeError for an unknown account_id")


# ── cancel_order's True/False contract ──────────────────────────────────────
# This is not a logging nicety. copy_engine._force_fill_cancel_then_place places
# a full-size REPLACEMENT on any True, so reporting True for an order that had
# already FILLED doubles the subscriber's position. False means "already
# terminal, place nothing"; an unresolvable state must RAISE, never return True.

class _FakeCancelOps:
    """order_v3 / order_v2 double. `details` is the sequence of get_order_detail
    responses to hand back, one per call."""

    def __init__(self, details, cancel_resp):
        self._details = list(details)
        self._cancel = cancel_resp
        self.cancels = 0

    def get_order_detail(self, account_id, coid):
        return self._details.pop(0) if self._details else _FakeResp(500, None)

    def cancel_order(self, account_id, coid):
        self.cancels += 1
        return self._cancel

    def cancel_option(self, account_id, coid):
        self.cancels += 1
        return self._cancel


class _FakeCancelTrade:
    def __init__(self, ops):
        self.order_v3 = ops
        self.order_v2 = ops


def _cancel_adapter(details, cancel_resp=None):
    a = _adapter()
    ops = _FakeCancelOps(details, cancel_resp or _FakeResp(200, {}))
    a._trade_client = lambda: _FakeCancelTrade(ops)  # type: ignore[method-assign]
    a._ops = ops  # type: ignore[attr-defined]
    return a


def _detail(status):
    return _FakeResp(200, {"category": "US_STOCK", "items": [{"order_status": status}]})


def test_cancel_returns_false_for_an_already_filled_order():
    a = _cancel_adapter([_detail("FILLED")])
    assert a.cancel_order("c1") is False
    assert a._ops.cancels == 0        # nothing to cancel, so nothing was sent


def test_cancel_returns_true_for_a_live_order():
    a = _cancel_adapter([_detail("SUBMITTED")])
    assert a.cancel_order("c1") is True


def test_unreadable_order_that_turns_out_filled_returns_false():
    """The regression. The first read fails, the cancel returns 200 anyway — and
    the order had already filled. Returning True here is what doubles the
    position, so the outcome is CONFIRMED by re-reading."""
    a = _cancel_adapter([_FakeResp(500, None), _detail("FILLED")])
    assert a.cancel_order("c1") is False


def test_unreadable_order_that_was_live_returns_true():
    a = _cancel_adapter([_FakeResp(500, None), _detail("CANCELLED")])
    assert a.cancel_order("c1") is True


def test_unconfirmable_cancel_raises_rather_than_claiming_success():
    """Cancel accepted but the order still can't be read (a throttle, most
    likely — the same reason the first read failed). 'Unknown' must not be
    reported as True: the caller's failure path leaves the mirror alone, which
    is the safe outcome."""
    a = _cancel_adapter([_FakeResp(500, None), _FakeResp(500, None)])
    try:
        a.cancel_order("c1")
    except RuntimeError as exc:
        assert "could not be confirmed" in str(exc)
        return
    raise AssertionError("expected a RuntimeError when the state can't be confirmed")


# ── place_order validates the BODY, not just the status line ────────────────
def test_place_accepts_the_documented_success_body():
    a = _adapter()
    a._assert_place_accepted(
        _FakeResp(200, {"client_order_id": "c1", "order_id": "WB99"}), "c1",
    )   # must not raise


def test_place_rejects_a_200_carrying_an_error_code():
    """A batch place endpoint can return 200 with a per-order failure. Treating
    that as success writes a SUBMITTED row whose broker_order_id doesn't exist —
    the reconciler can never resolve it and close-detection thinks the
    subscriber holds a position they never opened."""
    a = _adapter()
    try:
        a._assert_place_accepted(
            _FakeResp(200, [{"error_code": "INSUFFICIENT_BUYING_POWER",
                             "msg": "not enough cash"}]), "c1",
        )
    except RuntimeError as exc:
        assert "INSUFFICIENT_BUYING_POWER" in str(exc)
        return
    raise AssertionError("expected a RuntimeError for an error body")


def test_place_accepts_an_unfamiliar_body_rather_than_failing_the_order():
    """Conservative about shapes we don't recognise: a Webull response change
    should degrade to a log line, not refuse every order."""
    a = _adapter()
    a._assert_place_accepted(_FakeResp(200, {"something": "new"}), "c1")


def test_place_still_raises_on_a_non_200():
    a = _adapter()
    try:
        a._assert_place_accepted(_FakeResp(400, {"msg": "bad"}), "c1")
    except RuntimeError:
        return
    raise AssertionError("expected a RuntimeError for a non-200 place")


# ── batch order snapshot (one call instead of N) ────────────────────────────
class _FakeListOps:
    def __init__(self, resp):
        self._resp = resp
        self.calls = 0

    def list_today_orders(self, account_id, page_size=10):
        self.calls += 1
        return self._resp


class _FakeListTrade:
    def __init__(self, ops):
        self.order = ops


def test_orders_snapshot_keys_by_our_client_order_id():
    """We key every order we place by our own client_order_id, and that is what
    the broker_order_id column holds — so the snapshot must too."""
    a = _adapter()
    a._trade_client = lambda: _FakeListTrade(_FakeListOps(_FakeResp(200, {  # type: ignore[method-assign]
        "orders": [
            {"order_id": "WB1", "client_order_id": "c1",
             "items": [{"order_status": "FILLED", "filled_qty": "2",
                        "filled_price": "10.50"}]},
            {"order_id": "WB2", "client_order_id": "c2",
             "items": [{"order_status": "SUBMITTED", "filled_qty": "0"}]},
        ]
    })))
    snap = a.get_orders_snapshot()
    assert set(snap) == {"c1", "c2"}
    assert snap["c1"].status == OrderStatus.FILLED
    assert snap["c1"].filled_quantity == Decimal("2")
    assert snap["c1"].filled_avg_price == Decimal("10.50")
    assert snap["c2"].status == OrderStatus.SUBMITTED


def test_orders_snapshot_is_empty_on_failure_not_raising():
    """Best-effort by contract — the caller falls back to per-order reads."""
    a = _adapter()
    a._trade_client = lambda: _FakeListTrade(_FakeListOps(_FakeResp(429, None)))  # type: ignore[method-assign]
    assert a.get_orders_snapshot() == {}


def test_orders_snapshot_skips_rows_without_a_client_order_id():
    a = _adapter()
    a._trade_client = lambda: _FakeListTrade(_FakeListOps(_FakeResp(200, {  # type: ignore[method-assign]
        "orders": [{"order_id": "WB9", "items": [{"order_status": "FILLED"}]}]
    })))
    assert a.get_orders_snapshot() == {}


# ── REAL order payloads, captured live (2026-09-18) ─────────────────────────
# Webull spells the filled quantity DIFFERENTLY per endpoint, and both are live:
#
#   /openapi/trade/order/detail   -> {"orders": [{"status", "filled_quantity"}]}
#   Query Day Orders              -> {"orders": [{"items": [{"order_status",
#                                                            "filled_qty"}]}]}
#
# Only the second spelling was handled, so a detail read of a filled order
# returned status=FILLED with filled_quantity=0. That pairing is poison for the
# copy path: _closeable_quantity sums filled_quantity, so the subscriber looked
# FLAT while holding the position, the mirror SELL was stamped is_closing=False
# and went out as SELL_TO_OPEN, and Webull refused it —
# OPENAPI_POSITION_ORDER_INTENT_MISMATCH, "Close intent mismatches position
# direction". You cannot open a short in a contract you are already long.

_LIVE_DETAIL_BODY = {
    "client_order_id": "936f745b047a48b38bc2a2e79e0b1a07",
    "combo_order_id": "6SIGOV6G6RKO7CN4E1L14KLJI9",
    "combo_type": "NORMAL",
    "orders": [{
        "client_order_id": "936f745b047a48b38bc2a2e79e0b1a07",
        "order_id": "6SIGOV6G6RKO7CN4E1L14KLJI9",
        "status": "FILLED",
        "instrument_type": "OPTION",
        "side": "BUY",
        "position_intent": "BUY_TO_OPEN",
        "order_type": "MARKET",
        "total_quantity": "2",
        "filled_quantity": "2",
        "filled_price": "0.15",
        "time_in_force": "DAY",
        "legs": [{"symbol": "NIO", "option_type": "CALL",
                  "option_expire_date": "2026-09-18", "strike_price": "3.50"}],
    }],
}


def test_live_order_detail_reads_the_fill():
    """The regression: status parsed, quantity did not."""
    a = _adapter()
    a._trade_client = lambda: _FakeTrade(_FakeResp(200, _LIVE_DETAIL_BODY))  # type: ignore[method-assign]
    res = a.get_order("936f745b047a48b38bc2a2e79e0b1a07")
    assert res.status == OrderStatus.FILLED
    assert res.filled_quantity == Decimal("2")     # was 0 — the whole bug
    assert res.filled_avg_price == Decimal("0.15")


def test_live_order_detail_is_recognised_as_an_option():
    a = _adapter()
    parsed = a._fetch_detail(_FakeTrade(_FakeResp(200, _LIVE_DETAIL_BODY)), "c")
    assert parsed is not None and parsed[1] is True      # is_option


def test_day_orders_spelling_still_reads_the_fill():
    """The OTHER endpoint uses filled_qty inside items[]. Both must work — the
    batch snapshot reads this one, get_order reads the other."""
    a = _adapter()
    a._trade_client = lambda: _FakeListTrade(_FakeListOps(_FakeResp(200, {  # type: ignore[method-assign]
        "hasNext": False, "pageSize": 10,
        "orders": [{
            "order_id": "4RH6IN2QHG3BEH9M16582E0PM9",
            "client_order_id": "dc38e53e2b784430a15baad985bb64b4",
            "combo_type": "NORMAL",
            "items": [{"category": "US_OPTION", "order_status": "FILLED",
                       "filled_qty": "2", "filled_price": "0.15",
                       "qty": "2", "side": "BUY", "symbol": "NIO"}],
        }],
    })))
    snap = a.get_orders_snapshot()
    got = snap["dc38e53e2b784430a15baad985bb64b4"]
    assert got.status == OrderStatus.FILLED
    assert got.filled_quantity == Decimal("2")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll webull-adapter order tests passed.")


# ── a protective stop has to outlive the session ─────────────────────────────

def test_a_stop_is_gtc_not_day():
    """A DAY stop is cancelled at 16:00 ET, leaving the position unprotected
    overnight and pre-market — exactly when a gap happens. A stop exists to
    protect until it triggers, which may be days away."""
    a = _adapter()
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.SELL,
        order_type=OrderType.STOP, quantity=Decimal("2"), stop_price=Decimal("1.50"),
    )
    d = a._build_stock_order(req, "c-stop")
    assert d["order_type"] == "STOP_LOSS"
    assert d["time_in_force"] == "GTC"


def test_an_option_stop_is_gtc_and_closes():
    a = _adapter()
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION, symbol="SPY", side=OrderSide.SELL,
        order_type=OrderType.STOP, quantity=Decimal("2"), stop_price=Decimal("1.50"),
        option_expiry=date(2026, 9, 25), option_strike=Decimal("771"),
        option_right=OptionRight.CALL, is_closing=True,
    )
    d = a._build_option_order(req, "c-optstop")
    assert d["order_type"] == "STOP_LOSS"
    assert d["time_in_force"] == "GTC"
    assert d["position_intent"] == "SELL_TO_CLOSE"
    assert d["stop_price"] == "1.50"


def test_a_stop_limit_is_also_gtc():
    a = _adapter()
    req = BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.SELL,
        order_type=OrderType.STOP_LIMIT, quantity=Decimal("2"),
        stop_price=Decimal("1.50"), limit_price=Decimal("1.45"),
    )
    assert a._build_stock_order(req, "c-sl")["time_in_force"] == "GTC"


def test_ordinary_orders_still_expire_with_the_session():
    """Only stops get GTC. A stale entry must not sit working into the next day."""
    a = _adapter()
    for ot, extra in (
        (OrderType.MARKET, {}),
        (OrderType.LIMIT, {"limit_price": Decimal("1.50")}),
    ):
        req = BrokerOrderRequest(
            instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.BUY,
            order_type=ot, quantity=Decimal("1"), **extra,
        )
        assert a._build_stock_order(req, "c")["time_in_force"] == "DAY", ot
