"""Direct Webull (OpenAPI) adapter — real-time trade signal.

Talks to Webull's official OpenAPI (``webull-openapi-python-sdk``) for a
single account the trader OWNS, authenticated with that account owner's
``app_key``/``app_secret``. This exists so a master trader can connect
their Webull account directly and we stream their fills over gRPC in
~seconds (vs SnapTrade's minutes) — see ``services.webull_listener``.

Scope
-----
Used for READS on the trader's own account: ``verify_connection`` (listener
startup check) and ``get_positions`` (close_reconciler's trader-position
read). Subscriber EXECUTION still runs through SnapTrade, so the write
methods (``place_order`` / ``get_order`` / ``cancel_order``) are NOT wired
for direct-Webull and raise ``NotImplementedError`` — nothing in the copy
path calls them on a trader's account.

Gating
------
Inert unless ``settings.webull_direct_enabled`` is true: ``adapter_for``
only routes ``BrokerName.WEBULL`` here when the flag is on, and no such
accounts exist until a trader connects one. Default OFF ⇒ zero change to
existing SnapTrade/Alpaca/IBKR behaviour.

Credentials shape (Fernet-encrypted in ``broker_accounts.encrypted_credentials``)::

    {
      "app_key":    "<trader's Webull app key>",
      "app_secret": "<trader's Webull app secret>",
      "account_id": "<Webull account_id, NOT the account number>",
      "region_id":  "us"
    }

The Webull SDK is imported LAZILY inside methods, so importing this module
never requires the SDK to be installed — only environments that actually
use direct Webull need it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

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

# Webull order_status → our OrderStatus. The SDK's canonical set is
# SUBMITTED / PARTIAL_FILLED / FILLED / CANCELLED / FAILED (trade/common/
# order_status.py); the extra keys are defensive against REST/stream variants
# (matches services.webull_listener._WEBULL_STATUS).
_STATUS_MAP: dict[str, OrderStatus] = {
    "SUBMITTED": OrderStatus.SUBMITTED,
    "PENDING": OrderStatus.SUBMITTED,
    "PENDING_SUBMIT": OrderStatus.SUBMITTED,
    "WORKING": OrderStatus.ACCEPTED,
    "ACCEPTED": OrderStatus.ACCEPTED,
    "QUEUED": OrderStatus.ACCEPTED,
    "PARTIAL_FILLED": OrderStatus.PARTIALLY_FILLED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELED,
    "CANCELED": OrderStatus.CANCELED,
    "FAILED": OrderStatus.REJECTED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}

_TERMINAL_STATUSES = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
})


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def _suppress_sdk_file_logger(api_client: Any) -> None:
    """Stop the Webull SDK from creating ``./webull_trade_sdk.log``.

    ``TradeClient.__init__`` calls ``_init_logger`` which attaches a
    ``TimedRotatingFileHandler`` writing that file in the process CWD — but our
    hardened container runs with a read-only root filesystem, so the write dies
    with ``[Errno 30] Read-only file system`` and takes the connect/verify call
    down with it. ``_init_logger`` only sets up its handlers when BOTH
    ``_stream_logger_set`` and ``_file_logger_set`` are falsy, so pre-marking one
    True makes it skip file logging entirely. We use our own logging anyway, and
    this also avoids the SDK's unbounded log growth + app_key-in-file leak.
    """
    try:
        api_client._stream_logger_set = True  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass


def _is_writable_dir(path: str) -> bool:
    """True if ``path`` exists (or can be created) and is writable."""
    try:
        os.makedirs(path, exist_ok=True)
        return os.access(path, os.W_OK)
    except Exception:  # noqa: BLE001
        return False


def _resolve_token_base() -> str:
    """The base dir the SDK persists Webull tokens under. Prefers
    WEBULL_OPENAPI_TOKEN_DIR (default ``/data/webull_token`` — a durable volume
    in the prod container), but falls back to a writable temp dir when that base
    can't be written. Without the fallback, running direct Webull OUTSIDE the
    container (localhost / bare metal, where ``/data`` is on the read-only root)
    dies with ``ERROR_STORAGE_TOKEN [Errno 30] Read-only file system: '/data'``
    the moment the SDK tries to store a token. Prod is unaffected — ``/data`` is
    writable there, so the configured base is used as-is.
    """
    base = os.getenv("WEBULL_OPENAPI_TOKEN_DIR", "/data/webull_token")
    if _is_writable_dir(base):
        return base
    fallback = os.path.join(tempfile.gettempdir(), "webull_token")
    log.warning(
        "webull token dir %r is not writable; falling back to %r. Set "
        "WEBULL_OPENAPI_TOKEN_DIR to a durable writable path (a mounted volume) "
        "in production so tokens survive restarts.",
        base, fallback,
    )
    return fallback


def set_per_account_token_dir(api_client: Any, app_key: str | None) -> None:
    """Give each app_key its OWN token file so multiple Webull accounts don't
    collide. The SDK saves the verified token under a FIXED filename
    (``token.txt``) in one directory — so a second account (different app_key)
    loads the FIRST account's token → ``417 INVALID_TOKEN``. We point each
    app_key at its own subdirectory under the (durable) base token dir.
    ``set_token_dir`` takes priority over the WEBULL_OPENAPI_TOKEN_DIR env var.
    """
    base = _resolve_token_base()
    key_hash = hashlib.blake2b((app_key or "").encode(), digest_size=8).hexdigest()
    target = f"{base.rstrip('/')}/{key_hash}"
    # Pre-create the per-key dir so the SDK's write lands in an existing,
    # writable directory (some SDK versions don't makedirs before writing).
    try:
        os.makedirs(target, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        api_client.set_token_dir(target)
    except Exception:  # noqa: BLE001
        pass


# Cached TradeClient per app_key. TradeClient.__init__ runs the SDK's token
# flow (Create Token → 2FA). A new adapter is built per request (balance poll
# every ~30s, connect, close_reconciler), so building a fresh client each time
# re-ran the token flow and — during the pre-verify window — produced a fresh
# 2FA prompt every ~30s. We reuse ONE client per app_key so the token flow runs
# once per TTL; once the trader verifies (token → NORMAL, persisted on the
# shared volume) every path loads that token and never re-prompts. Mirrors
# services.webull_listener._webull_trade_client.
_TRADE_CLIENT_TTL_S = 1800.0
_trade_clients: dict[str, Any] = {}          # app_key -> (client, built_at)
_trade_client_lock = threading.Lock()


class WebullAdapter(BrokerAdapter):
    """One instance per Webull BrokerAccount. Credentials held in-memory only."""

    name = "webull"

    def __init__(self, credentials: dict[str, Any]):
        super().__init__(credentials)
        self.app_key = credentials.get("app_key")
        self.app_secret = credentials.get("app_secret")
        self.account_id = credentials.get("account_id")
        self.region_id = credentials.get("region_id", "us")

    # ── client construction (lazy SDK import, cached per app_key) ─────────
    def _trade_client(self):
        from webull.core.client import ApiClient          # noqa: PLC0415
        from webull.trade.trade_client import TradeClient  # noqa: PLC0415

        now = time.monotonic()
        with _trade_client_lock:
            cached = _trade_clients.get(self.app_key)
            if cached is not None and (now - cached[1]) < _TRADE_CLIENT_TTL_S:
                return cached[0]
            api_client = ApiClient(self.app_key, self.app_secret, self.region_id)
            _suppress_sdk_file_logger(api_client)
            set_per_account_token_dir(api_client, self.app_key)  # isolate token per app_key
            client = TradeClient(api_client)   # token flow runs HERE — once per TTL
            _trade_clients[self.app_key] = (client, now)
            return client

    # ── reads (used by the direct-Webull trader path) ────────────────────
    def verify_connection(self) -> ConnectionInfo:
        """Confirm the keys authenticate and the configured account_id exists.
        Raises with a user-surfaceable message on failure."""
        trade = self._trade_client()
        res = trade.account_v2.get_account_list()
        if getattr(res, "status_code", None) != 200:
            raise RuntimeError(f"Webull get_account_list failed: {getattr(res, 'status_code', '?')}")
        accounts = res.json() or []
        ids = {str(a.get("account_id")) for a in accounts if isinstance(a, dict)}
        if self.account_id and str(self.account_id) not in ids:
            raise RuntimeError(
                f"Webull account_id {self.account_id} not found for these keys "
                f"(available: {sorted(ids)})"
            )
        return ConnectionInfo(
            broker_account_id=str(self.account_id) if self.account_id else None,
            supports_fractional=False,   # Webull US options/stocks: whole units in copy path
            extra={"region_id": self.region_id},
        )

    def get_positions(self) -> list[BrokerPosition]:
        """Live positions for this account. Best-effort field mapping — the
        exact response shape must be validated against a real account before
        enabling ``webull_direct_enabled`` (this feeds close_reconciler)."""
        trade = self._trade_client()
        res = trade.account_v2.get_account_position(self.account_id)
        if getattr(res, "status_code", None) != 200:
            log.warning("webull get_account_position failed: %s", getattr(res, "status_code", "?"))
            return []
        body = res.json() or {}
        # Response is either a list of positions or a dict wrapping one.
        rows = body if isinstance(body, list) else (
            body.get("positions") or body.get("holdings") or body.get("items") or []
        )
        out: list[BrokerPosition] = []
        for p in rows:
            if not isinstance(p, dict):
                continue
            cat = str(_first(p, "category", "asset_type", "instrument_type") or "").upper()
            is_opt = "OPTION" in cat
            qty = _dec(_first(p, "quantity", "position", "units")) or Decimal(0)
            # Signed: short positions come back with a direction flag on some
            # brokers; default to long unless explicitly marked short.
            direction = str(_first(p, "direction", "side", "position_side") or "").upper()
            if direction in ("SHORT", "SELL") and qty > 0:
                qty = -qty
            sym = str(_first(p, "symbol", "ticker") or "").upper()
            out.append(BrokerPosition(
                broker_symbol=str(_first(p, "instrument_id", "broker_symbol", "symbol") or sym),
                symbol=sym,
                instrument_type=InstrumentType.OPTION if is_opt else InstrumentType.STOCK,
                quantity=qty,
                avg_entry_price=_dec(_first(p, "cost_price", "avg_price", "average_cost")),
                current_price=_dec(_first(p, "last_price", "market_price", "price")),
                market_value=_dec(_first(p, "market_value", "market_val")),
                unrealized_pnl=_dec(_first(p, "unrealized_pnl", "unrealized_profit_loss", "open_pnl")),
                cost_basis=_dec(_first(p, "cost_basis", "total_cost")),
            ))
        return out

    def get_balance_snapshot(self) -> dict[str, Any]:
        """Cash / buying power / equity for the Brokers UI + connect. Shape
        matches the Alpaca/SnapTrade adapters so ``_refresh_balance_into`` can
        consume it. Validated against a real Webull balance response."""
        trade = self._trade_client()
        res = trade.account_v2.get_account_balance(self.account_id)
        if getattr(res, "status_code", None) != 200:
            raise RuntimeError(f"webull get_account_balance failed: {getattr(res, 'status_code', '?')}")
        b = res.json() or {}
        assets = b.get("account_currency_assets") or []
        a0 = assets[0] if assets and isinstance(assets[0], dict) else {}
        return {
            "cash": _dec(b.get("total_cash_balance") or a0.get("cash_balance")),
            "buying_power": _dec(a0.get("buying_power") or a0.get("option_buying_power")),
            "total_equity": _dec(b.get("total_net_liquidation_value") or a0.get("net_liquidation_value")),
            "currency": b.get("total_asset_currency") or a0.get("currency") or "USD",
        }

    def get_pnl_snapshot(self) -> dict[str, Any] | None:
        """Equity / day-start / today's P&L for the daily kill switches. Webull
        reports today's P&L DIRECTLY (``total_day_profit_loss``), so day-start is
        derived as equity − todays_pl. Returns None (poller skips) on failure."""
        try:
            trade = self._trade_client()
            res = trade.account_v2.get_account_balance(self.account_id)
            if getattr(res, "status_code", None) != 200:
                return None
            b = res.json() or {}
            equity = _dec(b.get("total_net_liquidation_value"))
            todays_pl = _dec(b.get("total_day_profit_loss")) or Decimal(0)
            if equity is None:
                return None
            return {
                "todays_pl": todays_pl,
                "equity": equity,
                "beginning_day_balance": equity - todays_pl,
            }
        except Exception:  # noqa: BLE001
            log.warning("webull get_pnl_snapshot failed", exc_info=True)
            return None

    # ── writes — subscriber mirror execution on direct Webull ────────────
    # Order identity: Webull's cancel / replace / get_order_detail all key on
    # the CALLER-generated client_order_id (NOT the broker order_id), so we use
    # our Order row's UUID (stripped to Webull's 32-char max) as the
    # client_order_id AND return it as broker_order_id. Reusing the same
    # client_order_id across retries of one logical order is Webull's only
    # idempotency guard against double-placement.
    _ORDER_TYPE_MAP = {
        OrderType.MARKET: "MARKET",
        OrderType.LIMIT: "LIMIT",
        OrderType.STOP: "STOP_LOSS",
        OrderType.STOP_LIMIT: "STOP_LOSS_LIMIT",
    }

    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        if not self.account_id:
            raise RuntimeError("webull place_order: no account_id configured")
        trade = self._trade_client()
        coid = self._client_order_id(req)
        if req.instrument_type == InstrumentType.OPTION:
            resp = trade.order_v2.place_option(
                self.account_id, [self._build_option_order(req, coid)]
            )
        else:
            resp = trade.order_v3.place_order(
                self.account_id, [self._build_stock_order(req, coid)]
            )
        self._raise_for_status(resp, "place_order")
        # The place response returns only {client_order_id, order_id} — no fill
        # yet. Report SUBMITTED; the subscriber reconciler polls get_order for
        # the fill (exactly like the SnapTrade subscriber path).
        return BrokerOrderResult(
            broker_order_id=coid,
            status=OrderStatus.SUBMITTED,
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=Decimal(0),
            filled_avg_price=None,
        )

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        """Order status/fill for a mirror we placed. ``broker_order_id`` is the
        client_order_id we generated at placement (see place_order)."""
        trade = self._trade_client()
        detail = self._fetch_detail(trade, broker_order_id)
        if detail is None:
            raise RuntimeError(
                f"webull get_order_detail failed for {broker_order_id}"
            )
        _body, _is_opt, status, filled_qty, filled_px = detail
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status=status,
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=filled_qty,
            filled_avg_price=filled_px,
        )

    def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a working mirror. True = we cancelled a live order; False =
        the broker reports it already terminal (filled/cancelled) — nothing to
        cancel. Raises only when the order's state can't be resolved."""
        trade = self._trade_client()
        detail = self._fetch_detail(trade, broker_order_id)
        if detail is not None:
            _body, is_option, status, _q, _p = detail
            if status in _TERMINAL_STATUSES:
                return False
            resp = (
                trade.order_v2.cancel_option(self.account_id, broker_order_id)
                if is_option
                else trade.order_v3.cancel_order(self.account_id, broker_order_id)
            )
            if getattr(resp, "status_code", None) == 200:
                return True
            # Non-200: it may have filled/cancelled between the read and here.
            again = self._fetch_detail(trade, broker_order_id)
            if again is not None and again[2] in _TERMINAL_STATUSES:
                return False
            raise RuntimeError(f"webull cancel failed: {self._error_text(resp)}")
        # Couldn't read the order → instrument type unknown. Try both cancel
        # endpoints (a no-op on the wrong one); raise only if neither takes.
        for _do_cancel in (
            lambda: trade.order_v3.cancel_order(self.account_id, broker_order_id),
            lambda: trade.order_v2.cancel_option(self.account_id, broker_order_id),
        ):
            try:
                resp = _do_cancel()
            except Exception:  # noqa: BLE001
                continue
            if getattr(resp, "status_code", None) == 200:
                return True
        raise RuntimeError(
            f"webull cancel failed: order {broker_order_id} not found / not cancellable"
        )

    # ── order-build + response helpers ───────────────────────────────────
    @staticmethod
    def _client_order_id(req: BrokerOrderRequest) -> str:
        # Our Order UUID (dashes stripped → 32 hex) is stable per logical order,
        # so retries reuse it — Webull's idempotency key. Fall back to a fresh
        # uuid only when the caller supplied none.
        raw = req.client_order_id or uuid.uuid4().hex
        return raw.replace("-", "")[:32]

    @staticmethod
    def _fmt_qty(q: Decimal | Any) -> str:
        d = Decimal(str(q))
        if d == d.to_integral_value():
            return str(int(d))
        return format(d.normalize(), "f")

    @staticmethod
    def _fmt_price(p: Decimal | Any) -> str:
        d = Decimal(str(p))
        # Webull price precision: 2 decimals for >= $1, 4 decimals for < $1.
        step = Decimal("0.01") if abs(d) >= 1 else Decimal("0.0001")
        return str(d.quantize(step, rounding=ROUND_HALF_UP))

    @staticmethod
    def _session(req: BrokerOrderRequest) -> str:
        # support_trading_session: CORE = regular hours only; ALL = include
        # pre/post-market. A MARKET order must be CORE (extended hours is
        # limit-only). Options are RTH-only and carry no session field.
        if req.order_type == OrderType.MARKET:
            return "CORE"
        return "ALL" if req.extended_hours else "CORE"

    @staticmethod
    def _position_intent(req: BrokerOrderRequest) -> str:
        # Open vs close is a distinct field on Webull options — NOT encoded in
        # side alone. A closing SELL must be SELL_TO_CLOSE (never SELL_TO_OPEN),
        # or the broker rejects it "no position to close".
        buy = req.side == OrderSide.BUY
        if req.is_closing:
            return "BUY_TO_CLOSE" if buy else "SELL_TO_CLOSE"
        return "BUY_TO_OPEN" if buy else "SELL_TO_OPEN"

    def _build_stock_order(self, req: BrokerOrderRequest, coid: str) -> dict[str, Any]:
        d: dict[str, Any] = {
            "client_order_id": coid,
            "combo_type": "NORMAL",
            "symbol": req.symbol.upper(),
            "instrument_type": "STOCK",
            "market": "US",
            "side": "BUY" if req.side == OrderSide.BUY else "SELL",
            "order_type": self._ORDER_TYPE_MAP.get(req.order_type, "MARKET"),
            "quantity": self._fmt_qty(req.quantity),
            "time_in_force": "DAY",
            "entrust_type": "QTY",
            "support_trading_session": self._session(req),
        }
        if req.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and req.limit_price is not None:
            d["limit_price"] = self._fmt_price(req.limit_price)
        if req.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and req.stop_price is not None:
            d["stop_price"] = self._fmt_price(req.stop_price)
        return d

    def _build_option_order(self, req: BrokerOrderRequest, coid: str) -> dict[str, Any]:
        if not (req.option_expiry and req.option_strike and req.option_right):
            raise RuntimeError(
                "webull option order missing contract terms "
                "(expiry/strike/right required)"
            )
        intent = self._position_intent(req)
        side = "BUY" if req.side == OrderSide.BUY else "SELL"
        leg: dict[str, Any] = {
            "side": side,
            "position_intent": intent,
            "quantity": self._fmt_qty(req.quantity),
            "ratio": "1",
            "instrument_type": "OPTION",
            "market": "US",
            "symbol": req.symbol.upper(),
            "strike_price": self._fmt_price(req.option_strike),
            "option_expire_date": req.option_expiry.isoformat(),
            "option_type": "CALL" if req.option_right == OptionRight.CALL else "PUT",
        }
        d: dict[str, Any] = {
            "client_order_id": coid,
            "combo_type": "NORMAL",
            "option_strategy": "SINGLE",
            "order_type": self._ORDER_TYPE_MAP.get(req.order_type, "MARKET"),
            "quantity": self._fmt_qty(req.quantity),
            "time_in_force": "DAY",
            "entrust_type": "QTY",
            "position_intent": intent,
            "side": side,
            "legs": [leg],
        }
        if req.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and req.limit_price is not None:
            d["limit_price"] = self._fmt_price(req.limit_price)
        if req.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and req.stop_price is not None:
            d["stop_price"] = self._fmt_price(req.stop_price)
        return d

    def _fetch_detail(self, trade: Any, coid: str):
        """Query one order by client_order_id. Returns
        ``(body, is_option, status, filled_qty, filled_px)`` or None if the
        lookup itself failed (non-200 / no body)."""
        try:
            resp = trade.order_v3.get_order_detail(self.account_id, coid)
        except Exception:  # noqa: BLE001
            return None
        if getattr(resp, "status_code", None) != 200:
            return None
        body = resp.json() or {}
        order = body if isinstance(body, dict) else {}
        legs = (
            order.get("items") or order.get("legs")
            or order.get("orders") or order.get("order_legs") or []
        )
        leg = legs[0] if legs and isinstance(legs[0], dict) else order
        cat = str(
            _first(order, "category", "combo_ticker_type")
            or _first(leg, "category", "instrument_type") or ""
        ).upper()
        is_option = "OPTION" in cat
        status_raw = str(
            _first(leg, "order_status", "status")
            or _first(order, "order_status", "status") or ""
        ).upper()
        status = _STATUS_MAP.get(status_raw, OrderStatus.SUBMITTED)
        filled_qty = (
            _dec(_first(leg, "filled_qty", "filledQty", "cumulative_quantity"))
            or _dec(_first(order, "filled_qty", "filledQty"))
            or Decimal(0)
        )
        filled_px = (
            _dec(_first(leg, "filled_price", "avg_fill_price", "filledPrice", "avgFilledPrice"))
            or _dec(_first(order, "filled_price", "avg_fill_price"))
        )
        return order, is_option, status, filled_qty, filled_px

    def _raise_for_status(self, resp: Any, what: str) -> None:
        if getattr(resp, "status_code", None) != 200:
            raise RuntimeError(f"webull {what} failed: {self._error_text(resp)}")

    @staticmethod
    def _error_text(resp: Any) -> str:
        code = getattr(resp, "status_code", "?")
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = str(body.get("msg") or body.get("message") or body.get("code") or body)
            else:
                detail = str(body)
        except Exception:  # noqa: BLE001
            detail = str(getattr(resp, "text", "") or "")
        return f"HTTP {code} {detail}".strip()


__all__ = ["WebullAdapter"]
