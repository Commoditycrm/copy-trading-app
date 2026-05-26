"""Webull direct integration via the unofficial `webull` Python SDK.

What's actually possible here
-----------------------------
Webull does NOT issue API keys to retail users. The only practical path is
the reverse-engineered SDK (tedchou12/webull on PyPI), which logs in with
username + password + MFA and stores a session token. Caveats:

- Sessions expire (default ~1 day). We persist refresh tokens and call
  ``refresh_login()`` proactively.
- Placing orders requires a *second* secret — the 6-digit trade PIN.
  Webull issues a short-lived ``trade_token`` derived from it.
- No order-update websocket. The Webull mobile app uses MQTT internally,
  but the topic schema and credentials are undocumented and change. We
  poll ``get_current_orders()`` from app/services/webull_listener.py
  instead.

Credentials shape (Fernet-encrypted in broker_accounts.encrypted_credentials)::

    {
      "username": "user@example.com",
      "password": "...",
      "trade_pin": "123456",
      "paper": true,
      "region_code": 6,         # 6 = United States (Webull's enum)
      "device_id": "<uuid>",    # stable per-account device fingerprint
      "session": {
        "access_token":  "...",
        "refresh_token": "...",
        "token_expire":  "2026-05-27T12:00:00.000+0000",
        "uuid":          "...",
        "account_id":    "12345678",
        "trade_token":   "...",
        "did":           "<uuid>"
      }
    }

The session sub-dict is updated in-place after every successful login or
refresh. Username + password are kept so we can fully re-login when the
refresh token also expires; this is also why the encryption matters more
than for Alpaca (where a leaked API key can be revoked from the user's
dashboard — here a leak is a leak of the actual login).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from webull import paper_webull, webull as live_webull

from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.models.order import (
    InstrumentType,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
)

log = logging.getLogger(__name__)


# Map Webull → our enums. Webull's status strings are upper-snake; we keep
# the mapping loose because the SDK has surfaced minor wording changes
# across releases (e.g. "Working" vs "Working...").
_STATUS_IN = {
    "Working":         OrderStatus.ACCEPTED,
    "Pending":         OrderStatus.SUBMITTED,
    "Filled":          OrderStatus.FILLED,
    "Partial Filled":  OrderStatus.PARTIALLY_FILLED,
    "Partially Filled": OrderStatus.PARTIALLY_FILLED,
    "Cancelled":       OrderStatus.CANCELED,
    "Canceled":        OrderStatus.CANCELED,
    "Failed":          OrderStatus.REJECTED,
    "Rejected":        OrderStatus.REJECTED,
    "Expired":         OrderStatus.EXPIRED,
}

# Map our → Webull for order placement.
_SIDE_OUT = {OrderSide.BUY: "BUY", OrderSide.SELL: "SELL"}
_TYPE_OUT = {
    OrderType.MARKET: "MKT",
    OrderType.LIMIT: "LMT",
    OrderType.STOP: "STP",
    OrderType.STOP_LIMIT: "STP LMT",
}


def _dec_or_none(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _build_client(credentials: dict[str, Any]) -> Any:
    """Reconstruct a `webull` (or `paper_webull`) instance with persisted
    session state. Does NOT hit the network — only restores attributes.
    A subsequent call (get_account / refresh_login) will exercise auth."""
    paper = bool(credentials.get("paper", True))
    cls = paper_webull if paper else live_webull
    w = cls()
    sess = credentials.get("session") or {}
    w._access_token = sess.get("access_token", "")
    w._refresh_token = sess.get("refresh_token", "")
    w._token_expire = sess.get("token_expire", "")
    w._uuid = sess.get("uuid", "")
    w._account_id = sess.get("account_id", "")
    w._trade_token = sess.get("trade_token", "")
    did = sess.get("did") or credentials.get("device_id")
    if did:
        w._did = did
    region = credentials.get("region_code")
    if region is not None:
        w._region_code = region
    return w


def session_from_client(w: Any) -> dict[str, Any]:
    """Inverse of `_build_client` — pull the post-login session state out
    of a webull instance so the caller can re-encrypt it."""
    return {
        "access_token":  w._access_token,
        "refresh_token": w._refresh_token,
        "token_expire":  w._token_expire,
        "uuid":          w._uuid,
        "account_id":    w._account_id,
        "trade_token":   w._trade_token,
        "did":           w._did,
    }


def request_mfa(
    username: str,
    paper: bool = True,
    device_id: str | None = None,
) -> None:
    """Trigger Webull to send an MFA code to the username (SMS/email).

    ``device_id`` is critical: Webull binds each MFA request to the
    device fingerprint that requested it. If the subsequent login comes
    from a different ``_did``, Webull rejects the login with an empty
    response body — which crashes the SDK's ``response.json()`` parse
    and surfaces to the user as the cryptic ``Expecting value: line 1
    column 1 (char 0)`` JSONDecodeError. Callers MUST pass the same
    ``device_id`` they'll use for the follow-up login_with_mfa call."""
    import json as _json

    cls = paper_webull if paper else live_webull
    w = cls()
    if device_id:
        w._did = device_id
    try:
        with _install_response_logger():
            w.get_mfa(username)
    except _json.JSONDecodeError as exc:
        # Empty body from Webull → typically rate-limited or the account
        # doesn't exist. Surface a clean message instead of leaking the
        # parse error.
        raise ValueError(
            "Webull did not respond to the MFA request. Most common causes: "
            "the account isn't registered with Webull, or you've requested "
            "MFA too many times in a short window. Wait a few minutes and "
            "try again."
        ) from exc


class _RequestsProxy:
    """Proxy around the ``requests`` module that logs every POST/GET
    before returning to the caller. Installed in place of
    ``webull.webull.requests`` for the duration of a connect call.

    Why this shape: the webull SDK doesn't expose a Session — every
    method calls ``requests.post(...)`` against the module-level
    import. A Session-based interceptor (the previous approach) never
    fired. Swapping the module's `requests` reference is the smallest
    invasive change that captures the raw HTTP response before the
    SDK's ``response.json()`` call eats it on empty-body failures."""

    def __init__(self, real_requests: Any):
        self._real = real_requests

    def __getattr__(self, name: str) -> Any:
        # Pass through anything we don't explicitly wrap (exceptions,
        # status codes, etc.) so the SDK's other usages keep working.
        return getattr(self._real, name)

    def post(self, url: str, *args: Any, **kwargs: Any) -> Any:
        r = self._real.post(url, *args, **kwargs)
        _log_webull_response("POST", url, r)
        return r

    def get(self, url: str, *args: Any, **kwargs: Any) -> Any:
        r = self._real.get(url, *args, **kwargs)
        _log_webull_response("GET", url, r)
        return r


def _log_webull_response(method: str, url: str, r: Any) -> None:
    """Single log line per HTTP call. Truncated body so we don't spam
    the log if Webull ever returns a fat JSON payload."""
    text = getattr(r, "text", "") or ""
    log.info(
        "webull %s %s → %s (%d bytes) body=%s",
        method, url, getattr(r, "status_code", "?"),
        len(text), repr(text[:500]) if text else "<empty>",
    )


class _install_response_logger:
    """Context manager that swaps the ``requests`` module reference
    inside the webull SDK's source file with the logging proxy, for
    the duration of the ``with`` block. Restores on exit.

    Tricky bit: ``webull/__init__.py`` does ``from webull.webull import
    webull``, so ``import webull.webull`` resolves to the *class*, not
    the module. We have to grab the real module via ``sys.modules``."""

    def __init__(self) -> None:
        import sys
        # Force-import the submodule so it's in sys.modules. The
        # package's __init__ already imports it, but be defensive in
        # case a future refactor changes that.
        import webull.webull  # noqa: F401  (side-effect: registers in sys.modules)
        self._mod = sys.modules["webull.webull"]
        self._orig = None

    def __enter__(self) -> "_install_response_logger":
        self._orig = self._mod.requests
        self._mod.requests = _RequestsProxy(self._orig)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._orig is not None:
            self._mod.requests = self._orig


def login_with_mfa(
    *,
    username: str,
    password: str,
    mfa_code: str,
    trade_pin: str,
    paper: bool = True,
    device_id: str | None = None,
    region_code: int = 6,  # 6 = United States
) -> dict[str, Any]:
    """Run the full login flow and return a credentials dict ready to be
    Fernet-encrypted and stored on a BrokerAccount.

    ``device_id`` MUST match the value passed to ``request_mfa`` in the
    paired ``/start-mfa`` call. Otherwise Webull treats this as a
    different device and either fails the MFA exchange or requires a
    fresh code — both surface as empty-body JSON errors.

    Raises ``ValueError`` with a user-safe message on any failure
    (bad password, wrong MFA, expired MFA, bad trade PIN, device
    mismatch, account locked, …)."""
    import json as _json

    cls = paper_webull if paper else live_webull
    w = cls()
    if device_id:
        w._did = device_id
    if region_code:
        w._region_code = region_code

    # Diagnostic logging — see _install_response_logger. Cheap (one log
    # line per request) and makes 'why did Webull reject this?' debugging
    # tractable. Live accounts especially benefit because Webull's
    # anti-bot layer often returns 200 with an empty body instead of a
    # proper error code. We wrap each SDK method in the context manager
    # so the requests module monkey-patch is scoped tightly.
    try:
        with _install_response_logger():
            resp = w.login(
                username=username,
                password=password,
                device_name="copy-trading-platform",
                mfa=mfa_code,
                save_token=False,
            )
    except _json.JSONDecodeError as exc:
        # Webull returns an empty body on most auth failures (device
        # mismatch, bad password, expired MFA). The SDK then chokes on
        # response.json(). Translate to a useful message.
        mode = "live" if not paper else "paper"
        raise ValueError(
            f"Webull rejected the {mode} login (empty response from their server). "
            f"Check the backend log for the raw HTTP response — common causes:\n"
            f"  • MFA code expired or was already used (most common — re-request)\n"
            f"  • {'Live accounts often need a new-device confirmation email approval before API login works. Check your inbox.' if not paper else 'Account has a security question set — the unofficial SDK does not handle these.'}\n"
            f"  • Webull's anti-bot layer rate-limited this IP (wait 5–10 min and retry)\n"
            f"  • Account has a security question gate (unofficial SDK can't handle these — log in via the Webull app first to clear)"
        ) from exc

    # On success Webull returns a dict with accessToken. On a non-empty
    # failure it returns {"msg": "...", "code": "..."} and the SDK does
    # NOT raise — instance attrs stay empty.
    if not getattr(w, "_access_token", None):
        msg = (resp or {}).get("msg") if isinstance(resp, dict) else None
        code = (resp or {}).get("code") if isinstance(resp, dict) else None
        detail = f"{msg} (code={code})" if code else msg
        raise ValueError(detail or "Webull login failed (check password and MFA code)")

    # Exchange the trade PIN for a short-lived trade_token so we can place
    # orders. Without this, any place_order call returns "trade pin required".
    try:
        with _install_response_logger():
            w.get_trade_token(trade_pin)
    except _json.JSONDecodeError as exc:
        raise ValueError(
            "Webull rejected the trade PIN (empty response). Double-check "
            "the 6-digit PIN you set in Webull's mobile app under "
            "Account → Trading → Trade PIN."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Webull trade PIN rejected: {exc}") from exc
    if not getattr(w, "_trade_token", None):
        raise ValueError("Webull trade PIN accepted but no trade_token returned")

    return {
        "username":    username,
        "password":    password,
        "trade_pin":   trade_pin,
        "paper":       paper,
        "region_code": region_code,
        "device_id":   device_id or w._did,
        "session":     session_from_client(w),
    }


class WebullAdapter(BrokerAdapter):
    """One adapter per BrokerAccount. Holds the decrypted credentials and a
    lazily-constructed webull client. Not thread-safe — callers create a
    fresh adapter per request / per listener iteration."""

    name = "webull"

    def __init__(self, credentials: dict[str, Any]):
        self.credentials = credentials
        self._client: Any | None = None

    # ── internal ──────────────────────────────────────────────────────────

    def _c(self) -> Any:
        if self._client is None:
            self._client = _build_client(self.credentials)
        return self._client

    def _refresh_if_needed(self) -> None:
        """Best-effort token refresh. The SDK's refresh_login() is cheap;
        if it fails (refresh token expired), fall back to a full re-login
        using the stored username/password + trade PIN. MFA is NOT
        re-prompted here — Webull only requires MFA on new device IDs,
        and we persist `device_id` so we look like the same device."""
        w = self._c()
        try:
            w.refresh_login()
            self._persist_session()
            return
        except Exception:  # noqa: BLE001
            log.warning("webull refresh_login failed; attempting full re-login")
        # Full re-login. NB: this can fail with "MFA required" if Webull
        # decides our device looks suspicious — caller surfaces that to
        # the UI so the user can re-connect.
        creds = self.credentials
        try:
            w.login(
                username=creds["username"],
                password=creds["password"],
                device_name="copy-trading-platform",
                save_token=False,
            )
            w.get_trade_token(creds["trade_pin"])
            self._persist_session()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Webull session lost and re-login failed: {exc}") from exc

    def _persist_session(self) -> None:
        """Update self.credentials['session'] in-place from the live client
        attrs. Caller is responsible for re-encrypting + saving to DB."""
        self.credentials["session"] = session_from_client(self._c())

    # ── connection ────────────────────────────────────────────────────────

    def verify_connection(self) -> ConnectionInfo:
        a = self._c().get_account()
        # The SDK returns {"accounts": [...], ...} on the v3 endpoint or a
        # flat dict on v5; tolerant lookup.
        acct_id = self._c()._account_id or _attr(a, "secAccountId", "accountId", "id")
        currency = _attr(a, "currency", default="USD")
        return ConnectionInfo(
            broker_account_id=str(acct_id) if acct_id else None,
            supports_fractional=True,  # Webull supports fractional shares for stocks
            extra={"currency": currency, "raw_status": str(_attr(a, "status", default="active"))},
        )

    # ── orders ────────────────────────────────────────────────────────────

    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        if req.instrument_type != InstrumentType.STOCK:
            # Options are technically supported by the SDK but the contract
            # discovery flow is meaningfully different from Alpaca's OCC
            # symbols. Keep v1 stocks-only and surface a clean error so
            # subscribers aren't silently dropped.
            raise ValueError("Webull adapter v1 supports stocks only (options coming soon)")

        # Make sure we're authenticated before placing.
        if not self._c()._trade_token:
            self._refresh_if_needed()

        action = _SIDE_OUT[req.side]
        order_type = _TYPE_OUT[req.order_type]
        kwargs: dict[str, Any] = {
            "stock":     req.symbol.upper(),
            "action":    action,
            "orderType": order_type,
            "enforce":   "DAY",
            "quant":     float(req.quantity),
        }
        if req.limit_price is not None:
            kwargs["price"] = float(req.limit_price)
        if req.stop_price is not None:
            kwargs["stpPrice"] = float(req.stop_price)

        resp = self._c().place_order(**kwargs)
        # On success Webull returns {"orderId": "...", "success": true}.
        # On failure {"success": false, "msg": "..."}.
        if isinstance(resp, dict) and not resp.get("success", True):
            raise RuntimeError(resp.get("msg") or "Webull rejected order")
        order_id = (
            resp.get("orderId") if isinstance(resp, dict) else None
        ) or (resp.get("data", {}) or {}).get("orderId", "") if isinstance(resp, dict) else ""
        if not order_id:
            raise RuntimeError(f"Webull place_order returned no orderId: {resp!r}")
        return BrokerOrderResult(
            broker_order_id=str(order_id),
            status=OrderStatus.SUBMITTED,
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=Decimal(0),
            filled_avg_price=None,
        )

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        """Webull has no get-order-by-id endpoint exposed by the SDK. We
        scan recent open + history orders and pick the matching one. Used
        infrequently (status check / cancel cascade), so the O(N) scan is
        fine."""
        for o in self._iter_recent_orders():
            if str(_attr(o, "orderId", "id")) == str(broker_order_id):
                return self._order_to_result(o)
        raise LookupError(f"Webull order {broker_order_id} not found in recent history")

    def cancel_order(self, broker_order_id: str) -> None:
        self._c().cancel_order(broker_order_id)

    # ── positions ─────────────────────────────────────────────────────────

    def get_positions(self) -> list[BrokerPosition]:
        raw = self._c().get_positions() or []
        out: list[BrokerPosition] = []
        for p in raw:
            sym = str(_attr(p, "ticker", "symbol", default=""))
            qty = _dec_or_none(_attr(p, "position", "quantity")) or Decimal(0)
            # Webull doesn't sign quantity for short positions in the SDK's
            # default response; rely on `positionType` if present.
            if str(_attr(p, "positionType", default="")).upper() == "SHORT":
                qty = -qty
            out.append(BrokerPosition(
                broker_symbol=sym,
                symbol=sym,
                instrument_type=InstrumentType.STOCK,
                quantity=qty,
                avg_entry_price=_dec_or_none(_attr(p, "costPrice", "avgPrice")),
                current_price=_dec_or_none(_attr(p, "lastPrice", "currentPrice")),
                market_value=_dec_or_none(_attr(p, "marketValue", "value")),
                unrealized_pnl=_dec_or_none(_attr(p, "unrealizedProfitLoss", "unrealizedPnl")),
                cost_basis=_dec_or_none(_attr(p, "cost", "totalCost")),
            ))
        return out

    # ── reads — used by the listener + balance refresh ─────────────────────

    def get_balance_snapshot(self) -> dict[str, Any]:
        a = self._c().get_account()
        return {
            "cash":         _dec_or_none(_attr(a, "cashBalance", "settledFunds")),
            "buying_power": _dec_or_none(_attr(a, "dayBuyingPower", "buyingPower")),
            "total_equity": _dec_or_none(_attr(a, "netLiquidation", "totalAsset", "totalAccountValue")),
            "currency":     _attr(a, "currency", default="USD"),
        }

    def list_recent_activities(self) -> list[Any]:
        """List recent orders. Used by the listener's poll loop and the
        backfill pass after a session reconnects. Returns raw SDK dicts —
        listener code does the schema translation."""
        try:
            current = self._c().get_current_orders() or []
        except Exception:  # noqa: BLE001
            current = []
        try:
            history = self._c().get_history_orders(status="All", count=50) or []
        except Exception:  # noqa: BLE001
            history = []
        return list(current) + list(history)

    def _iter_recent_orders(self) -> list[Any]:
        """Same as list_recent_activities but typed as ordersonly — kept
        as a method so future filtering (e.g. by date) lives in one place."""
        return self.list_recent_activities()

    def _order_to_result(self, o: Any) -> BrokerOrderResult:
        raw_status = str(_attr(o, "status", "statusStr", default=""))
        return BrokerOrderResult(
            broker_order_id=str(_attr(o, "orderId", "id")),
            status=_STATUS_IN.get(raw_status, OrderStatus.SUBMITTED),
            submitted_at=_as_dt(_attr(o, "placedTime", "createTime")) or datetime.now(timezone.utc),
            filled_quantity=_dec_or_none(_attr(o, "filledQuantity", "filledQty")) or Decimal(0),
            filled_avg_price=_dec_or_none(_attr(o, "avgFilledPrice", "filledAvgPrice")),
        )


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Tolerant attribute/key lookup — Webull SDK responses are dicts but
    nested fields are sometimes objects. Mirrors fills_sync._attr."""
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
# Webull's order responses for options vary by SDK version + endpoint
# (current_orders vs history_orders). Fields we see::
#
#   {
#     "orderId":   "...",
#     "action":    "BUY",
#     "comboType": "NORMAL" | "OPTION" | "ROLLING",
#     "ticker": {
#       "symbol":      "AAPL  231215C00150000",
#       "tickerType":  "OPTION",       # or 2 (stock=1, option=2)
#       "optionType":  "call" | "put",
#       "strikePrice": "150.0",
#       "expireDate":  "2023-12-15",
#       "unSymbol":    "AAPL"
#     }
#   }
#
# Some shapes nest the option metadata at the top level (``optionContract``)
# instead of under ``ticker``. We probe both, OCC-parse the raw symbol as
# a backstop so a partial response still classifies correctly.


def parse_webull_order_symbol(order_obj: Any) -> dict[str, Any]:
    """Extract instrument metadata from a Webull order. Same return
    shape as ``app.brokers.snaptrade.parse_snaptrade_order_symbol`` so
    the two listeners use an identical integration pattern."""
    from datetime import date as _date

    from app.brokers.alpaca import _looks_like_occ, _parse_occ

    ticker_block = _attr(order_obj, "ticker", default={}) or {}
    option_block = _attr(order_obj, "optionContract", default={}) or {}

    raw_symbol_string = str(
        _attr(ticker_block, "symbol", "ticker")
        or _attr(option_block, "symbol", "ticker")
        or _attr(order_obj, "symbol", "ticker", default="")
    )

    ticker_type = str(
        _attr(ticker_block, "tickerType", "assetType")
        or _attr(order_obj, "tickerType", "assetType", "comboType", default="")
    ).upper()
    is_option = (
        "OPTION" in ticker_type
        or ticker_type == "2"
        or bool(option_block)
        or bool(_attr(ticker_block, "optionType"))
    )

    if not is_option:
        # OCC backstop for older shapes that flatten the symbol into a
        # single field.
        candidate = raw_symbol_string.replace(" ", "")
        if _looks_like_occ(candidate):
            parsed = _parse_occ(candidate)
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
            "symbol":          raw_symbol_string.upper(),
            "broker_symbol":   raw_symbol_string,
            "option_expiry":   None,
            "option_strike":   None,
            "option_right":    None,
        }

    underlying = str(
        _attr(ticker_block, "unSymbol", "underlyingSymbol")
        or _attr(option_block, "unSymbol", "underlyingSymbol")
        or ""
    ).upper()

    right_str = str(
        _attr(ticker_block, "optionType", "direction")
        or _attr(option_block, "optionType", "direction", default="")
    ).upper()
    right = (
        OptionRight.CALL if right_str.startswith("C")
        else OptionRight.PUT if right_str.startswith("P")
        else None
    )

    strike = _dec_or_none(
        _attr(ticker_block, "strikePrice", "strike")
        or _attr(option_block, "strikePrice", "strike")
    )

    expiry_str = (
        _attr(ticker_block, "expireDate", "expirationDate")
        or _attr(option_block, "expireDate", "expirationDate")
    )
    expiry = None
    if expiry_str:
        s = str(expiry_str)
        try:
            expiry = _date.fromisoformat(s[:10])
        except (ValueError, TypeError):
            expiry = None

    # OCC parse as backstop for any missing field.
    if not (underlying and expiry and strike and right):
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


def _as_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        # Webull uses milliseconds-since-epoch in most places.
        try:
            ts = float(v)
            if ts > 1e12:
                ts /= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
