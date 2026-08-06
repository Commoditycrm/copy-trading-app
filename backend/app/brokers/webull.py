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

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.models.order import InstrumentType

log = logging.getLogger(__name__)


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


class WebullAdapter(BrokerAdapter):
    """One instance per Webull BrokerAccount. Credentials held in-memory only."""

    name = "webull"

    def __init__(self, credentials: dict[str, Any]):
        super().__init__(credentials)
        self.app_key = credentials.get("app_key")
        self.app_secret = credentials.get("app_secret")
        self.account_id = credentials.get("account_id")
        self.region_id = credentials.get("region_id", "us")

    # ── client construction (lazy SDK import) ────────────────────────────
    def _trade_client(self):
        from webull.core.client import ApiClient          # noqa: PLC0415
        from webull.trade.trade_client import TradeClient  # noqa: PLC0415

        api_client = ApiClient(self.app_key, self.app_secret, self.region_id)
        return TradeClient(api_client)

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

    # ── writes — NOT wired for direct Webull (subscribers use SnapTrade) ──
    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        raise NotImplementedError(
            "Direct-Webull order placement is not enabled — subscriber orders "
            "execute via SnapTrade. Webull-direct is read/stream only for now."
        )

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        raise NotImplementedError(
            "Direct-Webull get_order is not wired — the trade-event stream "
            "drives order state for the direct-Webull trader signal."
        )


__all__ = ["WebullAdapter"]
