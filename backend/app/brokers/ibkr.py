"""Interactive Brokers — direct integration via IBKR's OAuth Web API.

Each user (trader OR subscriber) registers their OWN self-service OAuth
consumer in their IBKR Client Portal, generates an access token, and pastes
the four pieces of OAuth material + their IBKR account id into our connect
form. We sign every API call with their per-user credentials — no shared
app-level consumer, no IBKR third-party approval required.

Credentials shape (Fernet-encrypted in broker_accounts.encrypted_credentials)::

    {
      "consumer_key":         "...",   # OAuth 1.0a consumer key
      "signing_key":          "...",   # OAuth 1.0a consumer signing key
      "access_token":         "...",   # per-user access token
      "access_token_secret":  "...",   # per-user access token secret
      "account_id":           "U1234567",  # IBKR account number (e.g. U1234567)
      "paper":                false
    }

Why direct OAuth (not the Client Portal Gateway)
------------------------------------------------
We do NOT want every subscriber to run IBKR's Java gateway 24/7 on their
own machine. The OAuth Web API talks to IBKR's hosted endpoints with
per-user OAuth 1.0a signing — no local process — which is the only
realistic SaaS shape.

Status / scope
--------------
* Stocks: place_order / get_order / cancel_order / get_positions / poll loop.
* Options: placement NOT yet implemented (needs the option-chain / OCC
  resolution flow). Externally-placed option orders are still detected by
  the listener and parsed into our Order schema.
* Untested against live IBKR yet — this file is a first pass against
  IBKR's documented endpoint shapes. Expect minor adjustments once we
  exercise a real account:
    - Some ``/iserver/*`` endpoints may require IBKR's Live Session Token
      (LST) handshake before responding; if direct signed calls return
      401, we'll add the DH key exchange.
    - Order-body field names and the placement confirmation/reply chain
      have historically varied between API versions — verify against your
      registered consumer's docs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import requests
from requests_oauthlib import OAuth1

from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.models.order import (
    InstrumentType,
    OrderSide,
    OrderStatus,
    OrderType,
)

log = logging.getLogger(__name__)


BASE_URL = "https://api.ibkr.com/v1/api"

# IBKR order status → our enum. IBKR is inconsistent across endpoints
# (some endpoints return "PreSubmitted", others "PRESUBMITTED", others
# "Pre Submitted") so we normalise to UPPER_SNAKE before lookup.
_STATUS_IN: dict[str, OrderStatus] = {
    "PENDINGSUBMIT":    OrderStatus.PENDING,
    "PENDING_SUBMIT":   OrderStatus.PENDING,
    "PRESUBMITTED":     OrderStatus.SUBMITTED,
    "PRE_SUBMITTED":    OrderStatus.SUBMITTED,
    "SUBMITTED":        OrderStatus.SUBMITTED,
    "ACCEPTED":         OrderStatus.ACCEPTED,
    "FILLED":           OrderStatus.FILLED,
    "PARTIALLYFILLED":  OrderStatus.PARTIALLY_FILLED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "CANCELLED":        OrderStatus.CANCELED,
    "CANCELED":         OrderStatus.CANCELED,
    "REJECTED":         OrderStatus.REJECTED,
    "INACTIVE":         OrderStatus.REJECTED,
    "EXPIRED":          OrderStatus.EXPIRED,
}

# Our → IBKR placement enums.
_SIDE_OUT = {OrderSide.BUY: "BUY", OrderSide.SELL: "SELL"}
_TYPE_OUT = {
    OrderType.MARKET:     "MKT",
    OrderType.LIMIT:      "LMT",
    OrderType.STOP:       "STP",
    OrderType.STOP_LIMIT: "STP_LMT",
}


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Tolerant lookup — IBKR responses are mostly plain dicts, but field
    names vary between endpoints (orderId vs order_id vs id)."""
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _to_dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _norm_status(raw: Any) -> str:
    return str(raw or "SUBMITTED").upper().replace(" ", "_")


class IBKRAdapter(BrokerAdapter):
    """OAuth Web API client for ONE user's IBKR account.

    - Every HTTP call is signed with OAuth 1.0a (HMAC-SHA256) using the
      per-user consumer + token. requests-oauthlib does the signing.
    - IBKR identifies instruments by ``conid`` (an integer contract id),
      so every order needs a symbol→conid lookup first. Cached per
      process; restarts re-warm cheaply.
    """

    name = "ibkr"

    # Process-wide symbol→conid cache. Contract ids are stable so this is
    # safe to share across instances within one process.
    _conid_cache: dict[str, int] = {}

    def __init__(self, credentials: dict[str, Any]):
        super().__init__(credentials)
        self._consumer_key = credentials["consumer_key"]
        self._signing_key = credentials["signing_key"]
        self._access_token = credentials["access_token"]
        self._access_token_secret = credentials["access_token_secret"]
        self._account_id = credentials["account_id"]
        self._paper = bool(credentials.get("paper", False))

    # ── HTTP wrapper ──────────────────────────────────────────────────────

    def _oauth(self) -> OAuth1:
        return OAuth1(
            client_key=self._consumer_key,
            client_secret=self._signing_key,
            resource_owner_key=self._access_token,
            resource_owner_secret=self._access_token_secret,
            signature_method="HMAC-SHA256",
            signature_type="auth_header",
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{BASE_URL}{path}"
        kwargs.setdefault("timeout", 20)
        try:
            r = requests.request(method, url, auth=self._oauth(), **kwargs)
        except requests.RequestException as exc:
            raise RuntimeError(f"IBKR network error: {exc}") from exc
        if r.status_code == 401:
            raise RuntimeError(
                f"IBKR auth rejected (401) — verify the consumer key, "
                f"signing key, and access token are current. "
                f"body={r.text[:300]!r}"
            )
        if r.status_code >= 400:
            raise RuntimeError(
                f"IBKR {method} {path}: HTTP {r.status_code} — {r.text[:400]}"
            )
        if not r.text:
            return {}
        try:
            return r.json()
        except ValueError:
            return r.text

    # ── Account info / verify ─────────────────────────────────────────────

    def verify_connection(self) -> ConnectionInfo:
        """Lightweight authenticated call: list the accounts the OAuth
        token can see. If our stored ``account_id`` isn't among them,
        surface a clean message so the user can fix the form instead of
        having every subsequent order fail mysteriously."""
        body = self._request("GET", "/portfolio/accounts")
        accounts = body if isinstance(body, list) else (body.get("accounts") if isinstance(body, dict) else [])
        account_ids = {str(_attr(a, "accountId", "id")) for a in (accounts or []) if a}
        if self._account_id and self._account_id not in account_ids:
            raise RuntimeError(
                f"IBKR auth succeeded but account_id '{self._account_id}' "
                f"isn't in the connected accounts ({sorted(account_ids) or '[]'}). "
                "Re-check the account number on the connect form."
            )
        return ConnectionInfo(
            broker_account_id=self._account_id,
            # IBKR fractional-share support is symbol-specific and gated
            # by account permissions. Default off; subscribers can change
            # their broker_account.supports_fractional manually if they
            # know their account is enabled.
            supports_fractional=False,
            extra={"paper": self._paper, "accounts": sorted(account_ids)},
        )

    # ── Contract resolution (symbol → conid) ──────────────────────────────

    def _conid_for(self, symbol: str) -> int:
        sym = symbol.upper().strip()
        if sym in self._conid_cache:
            return self._conid_cache[sym]
        body = self._request("GET", "/iserver/secdef/search", params={"symbol": sym})
        if not isinstance(body, list) or not body:
            raise RuntimeError(f"IBKR symbol lookup empty for '{sym}'")
        # Prefer the U.S. stock whose ticker exactly matches; fall back to
        # the first match if no exact STK hit (rare).
        best = next(
            (h for h in body
             if (_attr(h, "secType") or "").upper() == "STK"
             and (_attr(h, "symbol") or "").upper() == sym),
            body[0],
        )
        conid = _attr(best, "conid", "conId")
        if conid is None:
            raise RuntimeError(f"IBKR symbol lookup for '{sym}' returned no conid: {best!r}")
        try:
            conid_int = int(conid)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"IBKR conid for '{sym}' is non-numeric: {conid!r}") from exc
        self._conid_cache[sym] = conid_int
        return conid_int

    # ── Orders ────────────────────────────────────────────────────────────

    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        if req.instrument_type != InstrumentType.STOCK:
            raise ValueError(
                "IBKR adapter: option order placement not yet implemented. "
                "Externally-placed options ARE detected by the listener; "
                "placement requires the option-chain resolution flow."
            )
        conid = self._conid_for(req.symbol)
        order: dict[str, Any] = {
            "acctId":    self._account_id,
            "conid":     conid,
            "secType":   "STK",
            "orderType": _TYPE_OUT.get(req.order_type, "MKT"),
            "side":      _SIDE_OUT[req.side],
            "quantity":  float(req.quantity),
            "tif":       "DAY",
        }
        if req.limit_price is not None:
            order["price"] = float(req.limit_price)
        if req.stop_price is not None:
            order["auxPrice"] = float(req.stop_price)
        if req.client_order_id:
            # IBKR caps custom-order-id length; truncate defensively.
            order["cOID"] = str(req.client_order_id)[:32]

        body = self._request(
            "POST",
            f"/iserver/account/{self._account_id}/orders",
            json={"orders": [order]},
        )
        # IBKR returns a list. Each item is either the placed order (with
        # ``order_id`` + ``order_status``) OR a confirmation prompt with
        # an ``id`` we must POST to /iserver/reply/{id}. Loop a few times
        # to clear any "Are you sure?" prompts before giving up.
        for _ in range(5):
            if not isinstance(body, list) or not body:
                raise RuntimeError(f"IBKR place_order: unexpected response {body!r}")
            first = body[0]
            order_status = _attr(first, "order_status", "orderStatus", "status")
            broker_order_id = _attr(first, "order_id", "orderId")
            if order_status is None and _attr(first, "id"):
                # Confirmation prompt — acknowledge and continue the loop.
                body = self._request(
                    "POST",
                    f"/iserver/reply/{_attr(first, 'id')}",
                    json={"confirmed": True},
                )
                continue
            if not broker_order_id:
                raise RuntimeError(f"IBKR place_order returned no order id: {first!r}")
            return BrokerOrderResult(
                broker_order_id=str(broker_order_id),
                status=_STATUS_IN.get(_norm_status(order_status), OrderStatus.SUBMITTED),
                submitted_at=datetime.now(timezone.utc),
                filled_quantity=Decimal(0),
                filled_avg_price=None,
            )
        raise RuntimeError("IBKR place_order: confirmation loop exceeded (5 prompts)")

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        """IBKR has no clean get-by-id; we scan the recent-orders feed
        (same source the listener uses). Matches WebullAdapter /
        SnapTradeAdapter pattern — this is called rarely (status checks
        / cancel cascade)."""
        for o in self.list_recent_activities():
            if str(_attr(o, "orderId", "order_id", "id") or "") == str(broker_order_id):
                return self._order_to_result(o)
        raise LookupError(f"IBKR order {broker_order_id} not in recent orders feed")

    def cancel_order(self, broker_order_id: str) -> None:
        try:
            self._request(
                "DELETE",
                f"/iserver/account/{self._account_id}/order/{broker_order_id}",
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"IBKR cancel_order: {exc}") from exc

    # ── Positions ─────────────────────────────────────────────────────────

    def get_positions(self) -> list[BrokerPosition]:
        out: list[BrokerPosition] = []
        page = 0
        while True:
            body = self._request(
                "GET", f"/portfolio/{self._account_id}/positions/{page}"
            )
            rows = body if isinstance(body, list) else (body.get("positions") if isinstance(body, dict) else [])
            if not rows:
                break
            for p in rows:
                qty = _to_dec(_attr(p, "position", "quantity")) or Decimal(0)
                if qty == 0:
                    continue
                symbol_raw = str(_attr(p, "contractDesc", "ticker", "symbol") or "")
                sec_type = (_attr(p, "secType", "assetClass") or "").upper()
                instrument = (
                    InstrumentType.OPTION if sec_type in ("OPT", "FOP")
                    else InstrumentType.STOCK
                )
                out.append(BrokerPosition(
                    broker_symbol=str(_attr(p, "conid", "conId") or symbol_raw),
                    # contractDesc for options reads like "AAPL 06JUN26 200 C" —
                    # the first token is the underlying, which is the friendly
                    # display value. For stocks it's just the ticker.
                    symbol=symbol_raw.split(" ")[0],
                    instrument_type=instrument,
                    quantity=qty,
                    avg_entry_price=_to_dec(_attr(p, "avgCost", "avg_cost", "avgPrice")),
                    current_price=_to_dec(_attr(p, "mktPrice", "marketPrice")),
                    market_value=_to_dec(_attr(p, "mktValue", "marketValue")),
                    unrealized_pnl=_to_dec(_attr(p, "unrealizedPnl")),
                    cost_basis=None,
                ))
            page += 1
            # IBKR returns pages of 100 per their convention; under-full
            # means we've hit the end. Cap defensively at 20 pages.
            if len(rows) < 100 or page > 20:
                break
        return out

    # ── Recent orders (for the listener poll) ─────────────────────────────

    def list_recent_activities(self) -> list[Any]:
        """Polled by ibkr_listener._poll_once. Returns raw IBKR order rows;
        the listener handles dedup, persistence, and fanout."""
        body = self._request("GET", "/iserver/account/orders")
        if isinstance(body, dict):
            orders = body.get("orders") or []
        elif isinstance(body, list):
            orders = body
        else:
            orders = []
        # IBKR can return orders for sibling sub-accounts; scope to ours
        # so we only see what belongs to this user's connected account.
        return [
            o for o in orders
            if not _attr(o, "acctId", "account")
            or _attr(o, "acctId", "account") == self._account_id
        ]

    # ── helpers ───────────────────────────────────────────────────────────

    def _order_to_result(self, o: Any) -> BrokerOrderResult:
        broker_order_id = str(_attr(o, "orderId", "order_id", "id") or "")
        status_str = _norm_status(_attr(o, "status", "orderStatus"))
        filled = _to_dec(_attr(o, "filledQuantity", "cumQty")) or Decimal(0)
        avg = _to_dec(_attr(o, "avgPrice", "lastPrice"))
        # IBKR returns timestamps as either ISO strings or epoch ms.
        ts = _attr(o, "lastExecutionTime", "submittedTime", "time")
        submitted_at = self._parse_ts(ts) or datetime.now(timezone.utc)
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status=_STATUS_IN.get(status_str, OrderStatus.SUBMITTED),
            submitted_at=submitted_at,
            filled_quantity=filled,
            filled_avg_price=avg,
        )

    @staticmethod
    def _parse_ts(v: Any) -> datetime | None:
        if v is None or v == "":
            return None
        if isinstance(v, (int, float)):
            # Treat anything beyond ~year 5000 in seconds as milliseconds.
            sec = v / 1000.0 if v > 10**12 else float(v)
            try:
                return datetime.fromtimestamp(sec, tz=timezone.utc)
            except (OSError, ValueError, OverflowError):
                return None
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
