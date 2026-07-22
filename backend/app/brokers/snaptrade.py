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
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
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

# Option (multi-leg) order-type strings. NOTE: the mleg endpoint uses the
# strict upper-case enum (MARKET/LIMIT/…), unlike the stock place_force_order
# path above which uses "Market"/"Limit".
_MLEG_TYPE_OUT = {
    OrderType.MARKET:     "MARKET",
    OrderType.LIMIT:      "LIMIT",
    OrderType.STOP:       "STOP_LOSS_MARKET",
    OrderType.STOP_LIMIT: "STOP_LOSS_LIMIT",
}


# Option premium tick sizes enforced by the exchanges (and validated by the
# brokerages behind SnapTrade, e.g. Webull, error code 1119): a premium under
# $3 must be in $0.05 increments, $3 and above in $0.10 increments. This is a
# BROKER constraint, so we enforce it here as the single chokepoint for every
# option order — no matter how the caller computed the price (a copied mirror
# that inherits a penny-quoted trader price, a re-anchored bracket exit, etc.).
_OPT_NICKEL = Decimal("0.05")
_OPT_DIME = Decimal("0.10")
_OPT_DIME_MIN = Decimal("3.00")


def _round_option_price_to_tick(price: Decimal) -> Decimal:
    """Snap an option premium to the nearest exchange-legal tick (nickel below
    $3, dime at/above $3). Rounding to the NEAREST valid tick keeps the drift
    under half a tick, and a legal tick is always accepted — so this only ever
    moves a price the broker would have rejected outright."""
    tick = _OPT_DIME if price >= _OPT_DIME_MIN else _OPT_NICKEL
    snapped = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    # A price like $2.98 snaps to $3.00, which is a legal dime — still fine.
    return snapped.quantize(Decimal("0.01"))


def _option_action(side: OrderSide, is_closing: bool) -> str:
    """(side, open/close) → SnapTrade single-leg option action enum."""
    if side == OrderSide.BUY:
        return "BUY_TO_CLOSE" if is_closing else "BUY_TO_OPEN"
    return "SELL_TO_CLOSE" if is_closing else "SELL_TO_OPEN"


def _occ_symbol_21(symbol: str, expiry: date, strike: Decimal, right: OptionRight) -> str:
    """Strict 21-char OCC option symbol required by SnapTrade's mleg API:
    6-char root (space-padded) + YYMMDD + C/P + 8-digit strike (price*1000).
    Example: AAPL 2026-06-19 $200 CALL -> 'AAPL  260619C00200000'.

    SnapTrade rejects the un-padded 19-char form that Alpaca accepts
    (error code 1012, "Invalid symbol length")."""
    root = symbol.upper().ljust(6)
    ymd = expiry.strftime("%y%m%d")
    cp = "C" if str(getattr(right, "value", right)).upper().startswith("C") else "P"
    strike_int = int((Decimal(strike) * 1000).to_integral_value(rounding=ROUND_HALF_UP))
    return f"{root}{ymd}{cp}{strike_int:08d}"


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

    @property
    def _authorization_id(self) -> str | None:
        return self.credentials.get("authorization_id")

    # ── connection ────────────────────────────────────────────────────────

    def force_resync(self) -> str:
        """Ask SnapTrade to re-pull this connection's data from the brokerage
        NOW, rather than waiting for its own background sync cadence.

        SnapTrade caches brokerage data and refreshes on its own schedule, so
        an order cancelled or filled DIRECTLY at the broker (e.g. on the Webull
        app, outside our app) can lag before it shows up in
        ``get_user_account_orders`` — which is what the listener polls. The
        refresh is asynchronous on SnapTrade's side, so the fresh data lands on
        a later poll, not this one.

        Returns one of:
          * ``"ok"``        — refresh accepted; fresh data lands on a later poll.
          * ``"forbidden"`` — this SnapTrade plan does NOT allow manual refresh
            (real-time plans serve live data already → 403, code 1141), OR no
            authorization id. The caller should STOP calling — retrying just
            burns 403s. The residual lag is upstream broker→SnapTrade and isn't
            fixable from our side on this plan.
          * ``"error"``     — transient failure (rate-limit/throttle/network);
            worth a retry on a later eligible tick.

        The CALLER MUST THROTTLE — never call this on every poll."""
        auth_id = self._authorization_id
        if not auth_id:
            return "forbidden"
        try:
            self._c().connections.refresh_brokerage_authorization(
                authorization_id=auth_id,
                user_id=self._user_id,
                user_secret=self._user_secret,
            )
            return "ok"
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status", None)
            if status == 403 or "1141" in str(getattr(exc, "body", "")):
                # Plan doesn't permit manual refresh (data is already
                # real-time). Permanent for this connection — tell the caller
                # to stop trying.
                log.info(
                    "snaptrade force_resync not permitted on this plan "
                    "(data already real-time): %s", exc
                )
                return "forbidden"
            # Rate-limited / transient — fine, the next eligible tick retries.
            log.info("snaptrade force_resync transient failure: %s", exc)
            return "error"

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

    def get_option_latest_quote(self, occ_symbol: str) -> tuple[Decimal | None, Decimal | None]:
        """Latest bid + ask for an OCC option symbol via SnapTrade.

        SnapTrade wants the 21-char OCC form **with spaces** padding the
        root to 6 chars (``AAPL  260608C00305000``). Our internal
        ``build_occ_symbol`` produces the no-space form Alpaca uses
        (``AAPL260608C00305000``) — we re-insert the padding here.

        Returns (bid, ask) as Decimals or (None, None) on any failure;
        the trade panel falls back to manual entry on null. We also
        treat 0/0 quotes as null (illiquid contracts late session).
        """
        if not occ_symbol or len(occ_symbol) < 16:
            return None, None
        # Split off the 15-char suffix (YYMMDD + C/P + 8-digit strike)
        # and pad the root to 6 chars with spaces.
        suffix = occ_symbol[-15:]
        root = occ_symbol[:-15]
        padded = f"{root:<6}{suffix}"
        try:
            resp = self._c().trading.get_user_account_option_quotes(
                user_id=self._user_id,
                user_secret=self._user_secret,
                account_id=self._account_id,
                symbol=padded,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "snaptrade.get_option_latest_quote(%s) failed: %s", occ_symbol, exc,
            )
            return None, None

        body = getattr(resp, "body", resp)
        # The response shape varies a bit by SDK version — try the most
        # common attribute names and fall through to None if none match.
        bid = _dec_or_none(
            _attr(body, "bid_price", "bidPrice", "bid")
        )
        ask = _dec_or_none(
            _attr(body, "ask_price", "askPrice", "ask")
        )
        if bid is not None and bid <= 0:
            bid = None
        if ask is not None and ask <= 0:
            ask = None
        log.info("snaptrade.get_option_latest_quote(%s) → bid=%s ask=%s",
                 occ_symbol, bid, ask)
        return bid, ask

    # ── orders ────────────────────────────────────────────────────────────

    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        # SnapTrade has no native bracket / OCO. Callers (trade endpoint,
        # copy_engine) now strip the bracket fields from the request for
        # non-Alpaca brokers — the emulator places the exit legs on fill,
        # see app/services/bracket_emulator.py. We assert the fields are
        # absent so a future regression that re-introduces them surfaces
        # immediately instead of silently dropping the SL/TP.
        assert req.take_profit_price is None and req.stop_loss_price is None, (
            "SnapTrade adapter should not receive bracket fields; the trade "
            "endpoint must strip them and let bracket_emulator handle the exits."
        )
        if req.instrument_type == InstrumentType.OPTION:
            return self._place_option_order(req)
        if req.instrument_type != InstrumentType.STOCK:
            raise ValueError(
                f"SnapTrade adapter: unsupported instrument_type {req.instrument_type}"
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

    def _place_option_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        """Place a single-leg option order via SnapTrade's multi-leg endpoint.

        SnapTrade identifies the contract by a strict 21-char OCC symbol
        (resolved server-side — no separate chain-discovery call needed) and
        requires prices as STRINGS. Only works on SnapTrade brokerages that
        support option trading (see support.snaptrade.com/brokerages)."""
        if not (req.option_expiry and req.option_strike and req.option_right):
            raise ValueError("option order missing expiry/strike/right")

        occ = _occ_symbol_21(
            req.symbol, req.option_expiry, req.option_strike, req.option_right
        )
        leg = {
            "action":     _option_action(req.side, req.is_closing),
            "instrument": {"symbol": occ, "instrument_type": "OPTION"},
            "units":      int(req.quantity),  # options trade in whole contracts
        }
        kwargs: dict[str, Any] = {
            "account_id":    self._account_id,
            "user_id":       self._user_id,
            "user_secret":   self._user_secret,
            "order_type":    _MLEG_TYPE_OUT.get(req.order_type, "LIMIT"),
            "time_in_force": "Day",
            "legs":          [leg],
        }
        # SnapTrade's mleg endpoint requires prices as strings, not numbers.
        # Snap to a legal option tick first (nickel <$3, dime >=$3) so a copied
        # or computed price can't be rejected for a bad increment (code 1119).
        if req.limit_price is not None:
            kwargs["limit_price"] = f"{_round_option_price_to_tick(req.limit_price)}"
        if req.stop_price is not None:
            kwargs["stop_price"] = f"{_round_option_price_to_tick(req.stop_price)}"

        try:
            resp = self._c().trading.place_mleg_order(**kwargs)
        except Exception as exc:  # noqa: BLE001
            # SnapTrade's ApiException str() leads with the status + a wall of
            # HTTP headers, which buries the response BODY — the actual
            # validation reason — past our 500-char reject_reason cap (and the
            # toast). Surface the body first so the real reason survives, and
            # log the full detail (untruncated) for debugging.
            status = getattr(exc, "status", None)
            reason = getattr(exc, "reason", None)
            body = getattr(exc, "body", None)
            log.warning(
                "SnapTrade place_mleg_order failed: status=%s reason=%s "
                "legs=%s order_type=%s limit=%s body=%s",
                status, reason, kwargs.get("legs"), kwargs.get("order_type"),
                kwargs.get("limit_price"), body,
            )
            detail = (str(body).strip() if body else str(reason or exc))[:300]
            raise RuntimeError(f"SnapTrade place_mleg_order: {detail}") from exc

        body = getattr(resp, "body", resp)
        order_id = _attr(body, "brokerage_order_id", "id", "trade_id")
        if not order_id:
            inner = _attr(body, "order", "orders")
            if isinstance(inner, (list, tuple)) and inner:
                inner = inner[0]
            order_id = _attr(inner, "brokerage_order_id", "id", "trade_id")
        if not order_id:
            raise RuntimeError(
                f"SnapTrade place_mleg_order returned no order id: {body!r}"
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

    def cancel_order(self, broker_order_id: str) -> bool:
        """SnapTrade's cancel endpoint takes brokerage_order_id.

        Returns True when we actually cancelled a working order; False when
        SnapTrade reports it was already terminal (the 1070 case below).

        Idempotent against terminal-state orders. When the underlying
        broker has already cancelled / filled / expired the order before
        SnapTrade's cancel call reaches it, SnapTrade returns 400 with
        ``code: '1070'`` and ``detail: "Failed to cancel order with
        provided brokerage_order_id"``. From the caller's perspective the
        desired end state ("order is no longer working") is already
        achieved, so we swallow that specific error and return cleanly
        instead of bubbling it up to the user as a hard broker error.

        Typical trigger: trader cancels the order in Webull's own app,
        then a few seconds later clicks Cancel on our Order History
        (because our DB hadn't synced the terminal status yet). Without
        this swallow they get a confusing "broker rejected" toast even
        though the actual broker state matches what they wanted.

        Swallowing 1070 is right for a user clicking Cancel, but not enough for
        cancel-then-REPLACE callers. 1070 means "I could not cancel it", and the
        usual reason is that it FILLED — so a replacement doubles the position.
        Prod did exactly that: a mirror filled at Webull, SnapTrade hadn't
        reported the fill yet, 1070 came back, we read it as success and placed
        a second order that also filled. Hence the bool — see
        copy_engine.force_fill_mirrors_to_market.

        After this returns True, the endpoint flips local status to
        CANCELED. If the broker had actually FILLED (not cancelled) the
        order, our DB row says CANCELED for the brief window until
        fills_sync's next tick corrects it.
        """
        try:
            self._c().trading.cancel_user_account_order(
                account_id=self._account_id,
                user_id=self._user_id,
                user_secret=self._user_secret,
                brokerage_order_id=broker_order_id,
            )
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            # SnapTrade's error envelope varies slightly across SDK
            # versions (quote style on the parsed body, presence of
            # the ``code`` key). Check both the numeric code and the
            # English detail to be robust.
            if "'code': '1070'" in err or '"code": "1070"' in err \
               or "Failed to cancel order with provided brokerage_order_id" in err:
                log.info(
                    "snaptrade cancel_order: broker_order_id=%s already in "
                    "terminal state (code 1070) — nothing was cancelled",
                    broker_order_id,
                )
                return False
            raise RuntimeError(f"SnapTrade cancel_order: {exc}") from exc
        return True

    # ── positions ─────────────────────────────────────────────────────────

    def get_positions(self) -> list[BrokerPosition]:
        """Return every open position on the account — stocks AND options.

        SnapTrade splits these across two endpoints:
          * ``account_information.get_user_account_positions`` → stocks /
            ETFs / crypto (no option positions even when the brokerage
            holds them).
          * ``options.list_option_holdings``                  → options.

        Both are called per tick; an empty list from either is fine.
        Failures on one don't suppress the other — we still surface
        whichever side returned data so the subscriber's positions UI
        and the per-position TP/SL enforcer have at least the partial
        view rather than nothing.
        """
        out: list[BrokerPosition] = []
        out.extend(self._get_stock_positions())
        out.extend(self._get_option_positions())
        return out

    def _get_stock_positions(self) -> list[BrokerPosition]:
        try:
            resp = self._c().account_information.get_user_account_positions(
                user_id=self._user_id,
                user_secret=self._user_secret,
                account_id=self._account_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("snaptrade get_user_account_positions failed")
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

    def _get_option_positions(self) -> list[BrokerPosition]:
        """Parse ``options.list_option_holdings`` into BrokerPosition rows.

        Response shape (from snaptrade_client.model.options_position):
          [
            {
              symbol: {
                option_symbol: {
                  ticker:           "AAPL  240621C00150000",  # OCC
                  option_type:      "CALL" | "PUT",
                  strike_price:     150.0,
                  expiration_date:  "2024-06-21",
                  underlying_symbol: { symbol: "AAPL", ... },
                },
                description: "AAPL Jun 21 2024 $150 Call",
              },
              price:                  4.25,   # contract price now
              units:                  5,      # contracts (NOT shares)
              average_purchase_price: 3.80,   # contract avg cost
              currency: { ... },
            },
            ...
          ]

        SnapTrade does NOT return market_value / unrealized_pnl / cost_basis
        on option holdings — we derive them from
        ``price * units * 100`` (and the cost equivalent), matching the
        OCC contract multiplier. The position TP/SL enforcer relies on
        these three fields, so computing them here is required (not just
        cosmetic).
        """
        try:
            resp = self._c().options.list_option_holdings(
                user_id=self._user_id,
                user_secret=self._user_secret,
                account_id=self._account_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("snaptrade list_option_holdings failed")
            return []
        body = getattr(resp, "body", resp) or []
        out: list[BrokerPosition] = []
        for p in body:
            sym_obj = _attr(p, "symbol", default={}) or {}
            opt_sym = _attr(sym_obj, "option_symbol", default={}) or {}
            occ = str(_attr(opt_sym, "ticker", default="")).strip()
            underlying = _attr(opt_sym, "underlying_symbol", default={}) or {}
            ticker = str(_attr(underlying, "symbol", "raw_symbol", default="")).upper()
            if not ticker and occ:
                # Fallback: ticker prefix of the OCC string (first non-digit chars).
                ticker = "".join(c for c in occ.split()[0] if c.isalpha()).upper()

            qty = _dec_or_none(_attr(p, "units", "quantity")) or Decimal(0)
            if qty == 0:
                continue

            current_price = _dec_or_none(_attr(p, "price", "last_price"))
            avg_entry = _dec_or_none(_attr(p, "average_purchase_price", "avgPrice"))
            strike = _dec_or_none(_attr(opt_sym, "strike_price"))
            expiry_raw = _attr(opt_sym, "expiration_date")
            try:
                from datetime import date as _date  # noqa: PLC0415
                if isinstance(expiry_raw, _date):
                    expiry = expiry_raw
                elif expiry_raw is not None:
                    expiry = _date.fromisoformat(str(expiry_raw)[:10])
                else:
                    expiry = None
            except ValueError:
                expiry = None
            right_raw = str(_attr(opt_sym, "option_type", default="")).upper()
            if right_raw.startswith("C"):
                right = OptionRight.CALL
            elif right_raw.startswith("P"):
                right = OptionRight.PUT
            else:
                right = None

            # OCC multiplier = 100 shares/contract. SnapTrade reports the two
            # price fields on DIFFERENT scales: `price` (current) is per-share,
            # but `average_purchase_price` is per-contract (already ×100). So
            # normalise avg to per-share before applying the multiplier — else
            # cost basis and P&L come out 100× too large, and the displayed
            # avg-entry doesn't line up with current_price. The TP/SL enforcer
            # depends on these too (it computes pct as unrealized_pnl / |cost_basis|).
            mult = Decimal(100)
            avg_per_share = avg_entry / mult if avg_entry is not None else None
            market_value = current_price * qty * mult if current_price is not None else None
            cost_basis = avg_per_share * qty * mult if avg_per_share is not None else None
            unrealized_pnl = (
                market_value - cost_basis
                if market_value is not None and cost_basis is not None
                else None
            )

            out.append(BrokerPosition(
                broker_symbol=occ or ticker,
                symbol=ticker,
                instrument_type=InstrumentType.OPTION,
                quantity=qty,
                avg_entry_price=avg_per_share,
                current_price=current_price,
                market_value=market_value,
                unrealized_pnl=unrealized_pnl,
                cost_basis=cost_basis,
                option_expiry=expiry,
                option_strike=strike,
                option_right=right,
            ))
        return out

    # ── reads — used by listener + balance refresh ─────────────────────────

    def get_pnl_snapshot(self) -> dict[str, Any] | None:
        """SnapTrade-routed: equity comes from the balance endpoint;
        the day-start figure (yesterday's close) is broker-dependent
        inside SnapTrade — we try several common field names on the
        account-details payload. When the broker doesn't surface a
        day-start, ``beginning_day_balance`` comes back None and the
        pct kill switch is effectively disabled for that subscriber
        (the loss/profit limits still work).

        Returns None on any failure so the poller skips this tick."""
        try:
            snap = self.get_balance_snapshot()
        except Exception:  # noqa: BLE001
            log.warning("snaptrade get_pnl_snapshot: balance fetch failed",
                        exc_info=True)
            return None
        cash = snap.get("cash")
        # Pull the account-details payload up front. Two things live here:
        # (a) ``balance.total.amount`` — the broker's authoritative view
        #     of the account's total value INCLUDING marked-to-market
        #     positions, when the brokerage supports it. This is far more
        #     accurate than using ``cash`` alone, which doesn't reflect
        #     open positions.
        # (b) day-start fields if the broker exposes them.
        # We make this one extra call regardless of whether (a) or (b)
        # are populated; failures here only degrade (a) to a cash-only
        # equity and (b) to None — the poller still runs.
        total_amount: Decimal | None = None
        beginning_day_balance: Decimal | None = None
        try:
            resp = self._c().account_information.get_user_account_details(
                user_id=self._user_id,
                user_secret=self._user_secret,
                account_id=self._account_id,
            )
            body = getattr(resp, "body", resp) or {}
            bal = _attr(body, "balance", default={}) or {}

            # (a) Broker-reported total. For Alpaca-paper-via-SnapTrade
            # this equals cash because paper accounts aren't being
            # mark-to-market by the broker. For live brokers it includes
            # position values and is what we want as equity.
            total_obj = _attr(bal, "total", default=None)
            if isinstance(total_obj, dict):
                total_amount = _dec_or_none(total_obj.get("amount"))
            else:
                total_amount = _dec_or_none(total_obj)

            # (b) Day-start, if exposed. SnapTrade brokers don't agree
            # on a field name — accept the union; first hit wins.
            raw = _attr(
                bal, "day_start_total", "beginning_of_day",
                "equity_previous_close", "previous_close",
            )
            if isinstance(raw, dict):
                raw = raw.get("amount")
            beginning_day_balance = _dec_or_none(raw)
        except Exception:  # noqa: BLE001
            log.warning(
                "snaptrade get_user_account_details failed — "
                "total_amount and beginning_day_balance will be None this tick",
                exc_info=True,
            )

        # Equity preference:
        #   1. balance.total.amount (broker's mark-to-market total)
        #   2. balance.total_equity from get_user_account_balance
        #      (some SDK versions surface it here)
        #   3. cash (last-resort fallback — paper accounts often have
        #      no other signal, and todays_pl will track only realized
        #      cash movement)
        equity = total_amount or snap.get("total_equity") or cash
        if equity is None:
            return None

        todays_pl = (
            equity - beginning_day_balance
            if beginning_day_balance is not None else Decimal(0)
        )
        return {
            "todays_pl":             todays_pl,
            "equity":                equity,
            "beginning_day_balance": beginning_day_balance,
        }

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
