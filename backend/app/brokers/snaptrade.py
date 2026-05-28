"""SnapTrade aggregator integration.

What it is
----------
SnapTrade is a hosted "OAuth for brokerages" service: the user clicks a
button in our UI, gets redirected to SnapTrade's connection portal,
picks their broker (Robinhood / E*TRADE / Tradier / Webull /
Schwab / …), and authenticates on SnapTrade's side. We never see the
broker credentials. We get back a per-connection ``authorization_id``
and one or more ``account_id``s we can use to read positions / orders
and submit trades.

Tradeoffs vs. our direct integrations
-------------------------------------
+ Many brokers via a single integration.
+ User credentials never touch our server.
- Order updates are POLLING-only — SnapTrade itself polls upstream
  brokers, so end-to-end latency is 10–60s in practice (vs <1s for
  Alpaca-direct, ~2–4s for Webull-direct).
- Costs money per connected user once you hit production volume.
- Order placement schema is normalised but loses some broker-specific
  features (e.g. bracket orders only work for a subset).

Credentials shape (Fernet-encrypted in broker_accounts.encrypted_credentials)::

    {
      "snaptrade_user_id":     "<our user.id, used as SnapTrade userId>",
      "snaptrade_user_secret": "<returned by register_snap_trade_user>",
      "authorization_id":      "<from list_brokerage_authorizations>",
      "account_id":            "<the SnapTrade account we'll trade on>",
      "brokerage_name":        "Robinhood",
      "brokerage_slug":        "ROBINHOOD"
    }

We deliberately keep ``snaptrade_user_secret`` Fernet-encrypted (not just
plain DB-protected) because anyone with it + the user_id can place
trades on any of the user's connected brokers via SnapTrade's API.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from snaptrade_client import SnapTrade

from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.config import get_settings
from app.models.order import (
    InstrumentType,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
)

log = logging.getLogger(__name__)


# SnapTrade's status enum strings → ours. Names are slightly different
# across endpoints (recent_orders vs orders); we accept both spellings.
_STATUS_IN = {
    "EXECUTED":         OrderStatus.FILLED,
    "FILLED":           OrderStatus.FILLED,
    "ACCEPTED":         OrderStatus.ACCEPTED,
    "PENDING":          OrderStatus.SUBMITTED,
    "SUBMITTED":        OrderStatus.SUBMITTED,
    "PARTIAL":          OrderStatus.PARTIALLY_FILLED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "CANCELLED":        OrderStatus.CANCELED,
    "CANCELED":         OrderStatus.CANCELED,
    "FAILED":           OrderStatus.REJECTED,
    "REJECTED":         OrderStatus.REJECTED,
    "EXPIRED":          OrderStatus.EXPIRED,
    "REPLACED":         OrderStatus.SUBMITTED,
}

# Our → SnapTrade enums for placement.
_SIDE_OUT = {OrderSide.BUY: "BUY", OrderSide.SELL: "SELL"}
_TYPE_OUT = {
    OrderType.MARKET:     "Market",
    OrderType.LIMIT:      "Limit",
    OrderType.STOP:       "Stop",
    OrderType.STOP_LIMIT: "StopLimit",
}


def _dec_or_none(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Tolerant lookup — SnapTrade SDK responses are dict-like but nested
    fields are sometimes typed pydantic objects. Same pattern as
    fills_sync._attr and webull._attr."""
    for n in names:
        if isinstance(obj, dict):
            v = obj.get(n)
        else:
            v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


# ── Option parsing ──────────────────────────────────────────────────────────
#
# SnapTrade returns options inside an order's symbol payload differently
# from stocks. The shape we see (across SDK versions / brokers) is roughly::
#
#   order.symbol.symbol = {
#     "symbol":      "AAPL  231215C00150000",  # OCC-ish, sometimes spaced
#     "raw_symbol":  "AAPL",
#     "type": {"code": "OPTION", "description": "Option"},
#     "option_symbol": {
#       "ticker":             "AAPL  231215C00150000",
#       "option_type":        "CALL" | "PUT",
#       "strike_price":       150.0,
#       "expiration_date":    "2023-12-15",
#       "underlying_symbol":  {"symbol": "AAPL"}
#     }
#   }
#
# Or for stocks::
#
#   order.symbol.symbol = {
#     "symbol":     "AAPL",
#     "raw_symbol": "AAPL",
#     "type": {"code": "cs", "description": "Common Stock"}
#   }
#
# Some brokers route options without the nested ``option_symbol`` block,
# embedding everything in the top-level ``symbol`` string instead. We
# fall back to OCC parsing for those — see app.brokers.alpaca._parse_occ
# which we reuse so the date/strike math stays in one place.


def parse_snaptrade_order_symbol(order_obj: Any) -> dict[str, Any]:
    """Extract the instrument-relevant fields from a SnapTrade order
    payload. Returns a dict with::

        {
          "instrument_type": InstrumentType.STOCK | InstrumentType.OPTION,
          "symbol":          "AAPL",          # underlying / display
          "broker_symbol":   "AAPL  231215C00150000",  # full broker id
          "option_expiry":   date or None,
          "option_strike":   Decimal or None,
          "option_right":    OptionRight or None,
        }

    Safe to call on any order; non-option rows just get the stock fields
    populated and option_* set to None.

    Handles TWO different SnapTrade response shapes:

      A. ``get_user_account_orders`` (broad history, what the listener uses):
         ``universal_symbol`` + ``option_symbol`` at the **top level** of
         the order. ``symbol`` is just a UUID string.

      B. ``get_user_account_recent_orders`` (narrower window):
         everything nested under ``symbol.symbol``, with ``option_symbol``
         buried inside.

    The first lookup that yields a non-empty block wins.
    """
    from app.brokers.alpaca import _looks_like_occ, _parse_occ

    # Shape A: flat top-level. Shape B: nested under symbol.symbol.
    top_universal = _attr(order_obj, "universal_symbol")
    top_option = _attr(order_obj, "option_symbol")

    if top_universal is not None or top_option is not None:
        # Shape A — use top-level blocks directly.
        sym_inner = top_universal or {}
        nested_option = top_option
    else:
        # Shape B — descend into symbol.symbol.
        sym_outer = _attr(order_obj, "symbol", default={})
        sym_inner = _attr(sym_outer, "symbol", default=sym_outer)
        nested_option = _attr(sym_inner, "option_symbol")

    # Primary signal: explicit type code on universal_symbol, plus the
    # presence of an option_symbol block.
    type_obj = _attr(sym_inner, "type", default={})
    type_code = str(_attr(type_obj, "code", "description", default="")).upper()
    is_option = "OPTION" in type_code or nested_option is not None

    raw_symbol_string = str(_attr(sym_inner, "symbol", "ticker", default=""))
    raw_root = str(_attr(sym_inner, "raw_symbol", default=raw_symbol_string)).upper()

    if not is_option:
        # Stock — but double-check the raw string in case the broker
        # dropped the ``type`` field and it's actually an OCC option.
        if _looks_like_occ(raw_symbol_string.replace(" ", "")):
            parsed = _parse_occ(raw_symbol_string.replace(" ", ""))
            if parsed is not None:
                root, expiry, strike, right = parsed
                return {
                    "instrument_type": InstrumentType.OPTION,
                    "symbol":          root,
                    "broker_symbol":   raw_symbol_string,
                    "option_expiry":   expiry,
                    "option_strike":   strike,
                    "option_right":    right,
                }
        return {
            "instrument_type": InstrumentType.STOCK,
            "symbol":          (raw_root or raw_symbol_string).upper(),
            "broker_symbol":   raw_symbol_string or raw_root,
            "option_expiry":   None,
            "option_strike":   None,
            "option_right":    None,
        }

    # Option path. Prefer the structured option_symbol block when present;
    # fall back to parsing the OCC string. ``nested_option`` was resolved
    # above to whichever of (top-level option_symbol, symbol.symbol.
    # option_symbol) actually held the data — see the shape selection
    # at the top of this function.
    opt = nested_option or {}
    underlying = str(
        _attr(_attr(opt, "underlying_symbol", default={}), "symbol", default="")
        or raw_root
    ).upper()

    expiry_str = _attr(opt, "expiration_date")
    expiry = _as_date(expiry_str)
    strike = _dec_or_none(_attr(opt, "strike_price"))
    right_str = str(_attr(opt, "option_type", default="")).upper()
    right = (
        OptionRight.CALL if right_str.startswith("C")
        else OptionRight.PUT if right_str.startswith("P")
        else None
    )

    # If the structured block didn't fill everything in, try OCC parsing
    # of the raw ticker as a backstop.
    if not (expiry and strike and right):
        candidate = raw_symbol_string.replace(" ", "")
        if _looks_like_occ(candidate):
            parsed = _parse_occ(candidate)
            if parsed is not None:
                occ_root, occ_expiry, occ_strike, occ_right = parsed
                underlying = underlying or occ_root
                expiry = expiry or occ_expiry
                strike = strike or occ_strike
                right = right or occ_right

    return {
        "instrument_type": InstrumentType.OPTION,
        "symbol":          underlying,
        "broker_symbol":   raw_symbol_string or underlying,
        "option_expiry":   expiry,
        "option_strike":   strike,
        "option_right":    right,
    }


def _as_date(v: Any):
    """Coerce SnapTrade's expiration_date strings into ``date``."""
    from datetime import date as _date
    if v is None:
        return None
    if isinstance(v, _date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    s = str(v)
    # SnapTrade emits ISO dates ("2023-12-15"); some brokers emit datetimes.
    try:
        return _date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def _build_client() -> SnapTrade:
    """Construct the SDK client from env config. Caller is responsible
    for surfacing a 503 if credentials are blank — we don't fail loudly
    here so adapter_for() can still build instances at import time."""
    s = get_settings()
    return SnapTrade(
        client_id=s.snaptrade_client_id,
        consumer_key=s.snaptrade_consumer_key,
    )


def snaptrade_configured() -> bool:
    """Used by API routes to gate the connect flow with a clean 503
    instead of letting the SDK fail with an opaque auth error."""
    s = get_settings()
    return bool(s.snaptrade_client_id and s.snaptrade_consumer_key)


def register_user(user_id: str) -> str:
    """Idempotently register a SnapTrade user and return the userSecret.

    SnapTrade refuses to re-register the same user_id, so we treat a
    409-style failure as 'already exists' and require the caller to
    have the userSecret cached. In our flow we generate user_secret on
    first connect and store it in the BrokerAccount; subsequent
    re-registers shouldn't happen for the same app user.
    """
    client = _build_client()
    resp = client.authentication.register_snap_trade_user(user_id=user_id)
    body = getattr(resp, "body", resp)
    secret = _attr(body, "userSecret", "user_secret")
    if not secret:
        raise RuntimeError(f"SnapTrade register returned no userSecret: {body!r}")
    return str(secret)


def make_login_url(
    *,
    user_id: str,
    user_secret: str,
    custom_redirect: str,
    broker_slug: str | None = None,
    connection_type: str = "trade",
) -> str:
    """Generate the connection portal URL. Caller redirects the user
    there; SnapTrade sends them back to ``custom_redirect`` after
    they finish (with a ``status`` query string).

    ``broker_slug`` (e.g. "ROBINHOOD") pre-selects a broker in the
    portal; pass None to let the user pick from SnapTrade's list.

    ``connection_type`` is the permission level requested. Default is
    ``"trade"`` because copy-trading is the whole point — subscribers
    need placement permission, and traders benefit from being able to
    cancel/close from inside Option Haven too. SnapTrade defaults to
    ``"read"`` when this argument is missing, which silently breaks
    every mirror order with a Forbidden response. If the chosen broker
    doesn't support trade through SnapTrade (Webull is read-only at
    SnapTrade's side, for example), the portal will downgrade to
    read automatically — we surface that via the authorization's
    ``type`` field after the user completes the flow."""
    client = _build_client()
    kwargs: dict[str, Any] = {
        "user_id":         user_id,
        "user_secret":     user_secret,
        "custom_redirect": custom_redirect,
        "connection_type": connection_type,
    }
    if broker_slug:
        kwargs["broker"] = broker_slug
    resp = client.authentication.login_snap_trade_user(**kwargs)
    body = getattr(resp, "body", resp)
    url = _attr(body, "redirectURI", "redirect_uri", "redirectUri")
    if not url:
        raise RuntimeError(f"SnapTrade login returned no redirectURI: {body!r}")
    return str(url)


def list_authorizations(user_id: str, user_secret: str) -> list[Any]:
    """Return the user's brokerage connections (one per broker the user
    has authorised through the portal)."""
    client = _build_client()
    resp = client.connections.list_brokerage_authorizations(
        user_id=user_id, user_secret=user_secret
    )
    body = getattr(resp, "body", resp)
    return list(body) if body else []


def list_accounts(user_id: str, user_secret: str) -> list[Any]:
    """Return every account across every connection. Caller filters by
    authorization_id when picking which one to attach."""
    client = _build_client()
    resp = client.account_information.list_user_accounts(
        user_id=user_id, user_secret=user_secret
    )
    body = getattr(resp, "body", resp)
    return list(body) if body else []


def delete_authorization(
    user_id: str, user_secret: str, authorization_id: str
) -> None:
    """Best-effort. Used by our DELETE endpoint so the user's SnapTrade
    side stays clean when they disconnect on our side."""
    client = _build_client()
    try:
        client.connections.remove_brokerage_authorization(
            authorization_id=authorization_id,
            user_id=user_id,
            user_secret=user_secret,
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "snaptrade delete_authorization(%s) failed — leaving orphan on their side",
            authorization_id,
        )


class SnapTradeAdapter(BrokerAdapter):
    name = "snaptrade"

    def __init__(self, credentials: dict[str, Any]):
        self.credentials = credentials
        self._client: SnapTrade | None = None

    def _c(self) -> SnapTrade:
        if self._client is None:
            self._client = _build_client()
        return self._client

    @property
    def _user_id(self) -> str:
        return self.credentials["snaptrade_user_id"]

    @property
    def _user_secret(self) -> str:
        return self.credentials["snaptrade_user_secret"]

    @property
    def _account_id(self) -> str:
        return self.credentials["account_id"]

    # ── connection ────────────────────────────────────────────────────────

    def verify_connection(self) -> ConnectionInfo:
        """Hit SnapTrade's balance endpoint as a cheap auth + alive check.
        Raises with a user-safe message on failure."""
        try:
            resp = self._c().account_information.get_user_account_balance(
                user_id=self._user_id,
                user_secret=self._user_secret,
                account_id=self._account_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"SnapTrade verify failed: {exc}") from exc
        body = getattr(resp, "body", resp)
        currency = _attr(_attr(body, "currency", default={}), "code", default="USD")
        return ConnectionInfo(
            broker_account_id=self._account_id,
            # SnapTrade exposes a fractional flag per brokerage; default
            # True is safer (rejects route to a clean error if the broker
            # actually doesn't support it; we don't want to silently
            # round down a 0.5-share trade to 0).
            supports_fractional=True,
            extra={
                "currency": currency,
                "brokerage_name": self.credentials.get("brokerage_name"),
            },
        )

    # ── orders ────────────────────────────────────────────────────────────

    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        if req.instrument_type != InstrumentType.STOCK:
            # Stock-orders only on the *placement* path. Detection of
            # externally-placed option orders (e.g. the trader places an
            # option on their broker's app, we observe it via the poll
            # loop) IS supported — see parse_snaptrade_order_symbol in
            # this module and snaptrade_listener._insert_order_from_
            # snaptrade. Placing options through SnapTrade requires a
            # universal_symbol_id lookup against their options
            # discovery endpoint, which is a meaningfully different
            # contract-resolution flow than Alpaca's OCC symbols and
            # needs its own work.
            raise ValueError(
                "SnapTrade adapter: option order placement not yet supported. "
                "Externally-placed option orders ARE detected and mirror "
                "correctly; the placement path needs the SnapTrade options "
                "discovery flow built out."
            )

        action = _SIDE_OUT[req.side]
        order_type = _TYPE_OUT[req.order_type]
        kwargs: dict[str, Any] = {
            "account_id":    self._account_id,
            "user_id":       self._user_id,
            "user_secret":   self._user_secret,
            "action":        action,
            "order_type":    order_type,
            "time_in_force": "Day",
            "symbol":        req.symbol.upper(),
            "units":         float(req.quantity),
        }
        if req.limit_price is not None:
            kwargs["price"] = float(req.limit_price)
        if req.stop_price is not None:
            kwargs["stop"] = float(req.stop_price)

        try:
            resp = self._c().trading.place_force_order(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"SnapTrade place_force_order: {exc}") from exc

        body = getattr(resp, "body", resp)
        order_id = _attr(body, "brokerage_order_id", "id", "trade_id")
        if not order_id:
            raise RuntimeError(
                f"SnapTrade place_force_order returned no brokerage_order_id: {body!r}"
            )
        status_str = str(_attr(body, "status", default="SUBMITTED")).upper()
        return BrokerOrderResult(
            broker_order_id=str(order_id),
            status=_STATUS_IN.get(status_str, OrderStatus.SUBMITTED),
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=Decimal(0),
            filled_avg_price=None,
        )

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        """SnapTrade has no get-by-id endpoint; the SDK exposes the
        canonical list-with-filter via get_user_account_orders. We scan
        recent orders (last ~50) and pick the match. Same pattern as
        WebullAdapter — fine because this is called infrequently
        (status check / cancel cascade)."""
        for o in self.list_recent_activities():
            if str(_attr(o, "brokerage_order_id", "id")) == str(broker_order_id):
                return self._order_to_result(o)
        raise LookupError(
            f"SnapTrade order {broker_order_id} not found in recent history"
        )

    def cancel_order(self, broker_order_id: str) -> None:
        """SnapTrade's cancel endpoint takes brokerage_order_id."""
        try:
            self._c().trading.cancel_user_account_order(
                account_id=self._account_id,
                user_id=self._user_id,
                user_secret=self._user_secret,
                brokerage_order_id=broker_order_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"SnapTrade cancel_order: {exc}") from exc

    # ── positions ─────────────────────────────────────────────────────────

    def get_positions(self) -> list[BrokerPosition]:
        try:
            resp = self._c().account_information.get_user_account_positions(
                user_id=self._user_id,
                user_secret=self._user_secret,
                account_id=self._account_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("snaptrade get_positions failed")
            return []
        body = getattr(resp, "body", resp) or []
        out: list[BrokerPosition] = []
        for p in body:
            sym_obj = _attr(p, "symbol", default={})
            # SnapTrade nests: position.symbol.symbol.symbol → ticker string
            inner_sym = _attr(sym_obj, "symbol", default={})
            ticker = str(_attr(inner_sym, "symbol", "raw_symbol", default="")).upper()
            qty = _dec_or_none(_attr(p, "units", "quantity")) or Decimal(0)
            out.append(BrokerPosition(
                broker_symbol=ticker,
                symbol=ticker,
                instrument_type=InstrumentType.STOCK,
                quantity=qty,
                avg_entry_price=_dec_or_none(_attr(p, "average_purchase_price", "avgPrice")),
                current_price=_dec_or_none(_attr(p, "price", "last_price")),
                market_value=_dec_or_none(_attr(p, "market_value")),
                unrealized_pnl=_dec_or_none(_attr(p, "open_pnl", "unrealized_pnl")),
                cost_basis=_dec_or_none(_attr(p, "book_value", "cost_basis")),
            ))
        return out

    # ── reads — used by listener + balance refresh ─────────────────────────

    def get_balance_snapshot(self) -> dict[str, Any]:
        """Pull cash/buying_power/total from SnapTrade.

        SnapTrade's ``get_user_account_balance`` returns a **list** of
        per-currency balance objects (one entry per currency the account
        holds — usually just USD). Each entry looks like::

            {"currency": {"code": "USD", ...},
             "cash": 10000.0, "buying_power": 40000.0}

        Older docs / some endpoints wrap balances in a dict with
        ``cash``/``buying_power`` at the top level, so we tolerate both
        shapes. Multi-currency accounts collapse to the first currency —
        our model has a single ``currency`` column so we can't represent
        all of them; the user sees their primary currency."""
        resp = self._c().account_information.get_user_account_balance(
            user_id=self._user_id,
            user_secret=self._user_secret,
            account_id=self._account_id,
        )
        body = getattr(resp, "body", resp)

        # New shape (current SDK): list of per-currency balances.
        if isinstance(body, list):
            primary = body[0] if body else {}
        else:
            primary = body or {}

        def _val(x: Any) -> Decimal | None:
            # SnapTrade has used both ``{"amount": ..., "currency": ...}``
            # objects and bare numbers across SDK versions. Tolerate both.
            if isinstance(x, dict):
                return _dec_or_none(x.get("amount"))
            return _dec_or_none(x)

        # ``total_value`` only appears on a subset of brokers. For brokers
        # that don't report it (Webull is one), leave it None — the UI
        # renders "—" gracefully and the user can compute it manually from
        # cash + positions.
        return {
            "cash":         _val(_attr(primary, "cash")),
            "buying_power": _val(_attr(primary, "buying_power")),
            "total_equity": _val(_attr(primary, "total_value", "total_equity")),
            "currency":     _attr(_attr(primary, "currency", default={}), "code", default="USD"),
        }

    def list_recent_activities(self) -> list[Any]:
        """Recent orders — listener polls this.

        We use ``get_user_account_orders`` (full order history) rather
        than ``get_user_account_recent_orders``. Even though "recent"
        sounds like what we want, in practice it returns empty for
        several brokers (Webull confirmed) — the brokers only populate
        the broader history endpoint. The full history is also a
        different schema: ``universal_symbol`` / ``option_symbol``
        live at the top level instead of nested under
        ``symbol.symbol``. ``parse_snaptrade_order_symbol`` handles
        both shapes.

        Dedup on (broker_order_id, status) at the listener layer
        means re-pulling the full history every 5s is correct, just
        slightly wasteful. Acceptable cost for not silently dropping
        orders on brokers whose ``recent_orders`` is empty."""
        try:
            resp = self._c().account_information.get_user_account_orders(
                user_id=self._user_id,
                user_secret=self._user_secret,
                account_id=self._account_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("snaptrade list_recent_activities (orders) failed")
            return []
        body = getattr(resp, "body", resp) or []
        # ``get_user_account_orders`` returns a bare list. Older SDK
        # versions wrap in a dict — tolerate both for safety.
        if isinstance(body, dict):
            return list(body.get("orders") or [])
        return list(body)

    def _order_to_result(self, o: Any) -> BrokerOrderResult:
        raw_status = str(_attr(o, "status", default="")).upper()
        sym_obj = _attr(o, "symbol", default={})
        ticker = str(_attr(_attr(sym_obj, "symbol", default={}),
                           "symbol", "raw_symbol", default="")).upper()
        return BrokerOrderResult(
            broker_order_id=str(_attr(o, "brokerage_order_id", "id")),
            status=_STATUS_IN.get(raw_status, OrderStatus.SUBMITTED),
            submitted_at=_as_dt(_attr(o, "time_placed", "created_at")) or datetime.now(timezone.utc),
            filled_quantity=_dec_or_none(_attr(o, "filled_units", "filled_quantity")) or Decimal(0),
            filled_avg_price=_dec_or_none(_attr(o, "execution_price", "filled_avg_price")),
        )


def _as_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
