"""SnapTrade client wrapper.

One SnapTrade user per app User. We reuse the app User.id (a UUID) as the
SnapTrade userId, and store the per-user userSecret encrypted on the users row.

All SnapTrade SDK calls go through here so the rest of the app doesn't import
the SDK directly.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from snaptrade_client import SnapTrade

from app.config import get_settings
from app.services.crypto import decrypt_json, encrypt_json


@lru_cache
def _client() -> SnapTrade:
    s = get_settings()
    return SnapTrade(
        consumer_key=s.snaptrade_consumer_key,
        client_id=s.snaptrade_client_id,
    )


def encrypt_secret(user_secret: str) -> str:
    return encrypt_json({"s": user_secret})


def decrypt_secret(blob: str) -> str:
    return decrypt_json(blob)["s"]


@dataclass
class SnapTradeIdentity:
    user_id: str
    user_secret: str


def register_user(app_user_id: uuid.UUID) -> SnapTradeIdentity:
    """Register an app user with SnapTrade. Idempotent on the SDK side: if the
    user already exists, SnapTrade returns the existing record (some SDK
    versions raise on duplicate — caller should handle by storing once)."""
    user_id = str(app_user_id)
    resp = _client().authentication.register_snap_trade_user(user_id=user_id)
    body: Any = resp.body if hasattr(resp, "body") else resp
    user_secret = body["userSecret"] if isinstance(body, dict) else body.user_secret
    return SnapTradeIdentity(user_id=user_id, user_secret=user_secret)


def login_redirect_uri(
    app_user_id: uuid.UUID, user_secret: str, return_url: str
) -> str:
    """Generate a one-time URL to SnapTrade's Connection Portal. The user opens
    it, picks a brokerage, authenticates with the broker, and is redirected
    back to `return_url`."""
    # connection_type="trade" requests read+trade access; default is read-only,
    # which would let us see positions but never place orders.
    resp = _client().authentication.login_snap_trade_user(
        user_id=str(app_user_id),
        user_secret=user_secret,
        custom_redirect=return_url,
        connection_type="trade",
    )
    body: Any = resp.body if hasattr(resp, "body") else resp
    if isinstance(body, dict):
        return body.get("redirectURI") or body["redirect_uri"]
    return body.redirect_uri


def list_accounts(app_user_id: uuid.UUID, user_secret: str) -> list[dict[str, Any]]:
    resp = _client().account_information.list_user_accounts(
        user_id=str(app_user_id), user_secret=user_secret
    )
    body: Any = resp.body if hasattr(resp, "body") else resp
    if isinstance(body, list):
        return body
    return body  # SDK returns list


def get_account_balance(
    app_user_id: uuid.UUID, user_secret: str, snaptrade_account_id: str
) -> dict[str, Any]:
    """Fetch the latest balance for one account.

    SnapTrade returns a list of per-currency balances + a total_value object.
    We normalize to a flat dict the API/UI can consume:
        {"cash": Decimal, "buying_power": Decimal, "total_equity": Decimal, "currency": "USD"}
    Any field SnapTrade doesn't return comes back as None.
    """
    from decimal import Decimal

    resp = _client().account_information.get_user_account_balance(
        user_id=str(app_user_id),
        user_secret=user_secret,
        account_id=snaptrade_account_id,
    )
    body: Any = resp.body if hasattr(resp, "body") else resp

    # Pick the first balance entry (single-currency accounts) — multi-currency
    # accounts would need to expose all entries; out of scope for v1.
    first = body[0] if isinstance(body, list) and body else (body or {})

    def _dec(v: Any) -> Decimal | None:
        if v is None:
            return None
        try:
            return Decimal(str(v))
        except Exception:  # noqa: BLE001
            return None

    cash = first.get("cash") if isinstance(first, dict) else None
    buying_power = first.get("buying_power") if isinstance(first, dict) else None
    currency = None
    if isinstance(first, dict):
        cur_obj = first.get("currency")
        if isinstance(cur_obj, dict):
            currency = cur_obj.get("code")
        elif isinstance(cur_obj, str):
            currency = cur_obj

    # total_equity isn't in get_user_account_balance — it's in get_user_account_details.
    # Pull it separately so the UI can show net liquidation value.
    total_equity = None
    try:
        det = _client().account_information.get_user_account_details(
            user_id=str(app_user_id),
            user_secret=user_secret,
            account_id=snaptrade_account_id,
        )
        det_body = det.body if hasattr(det, "body") else det
        if isinstance(det_body, dict):
            balance_obj = det_body.get("balance") or {}
            total_obj = balance_obj.get("total") if isinstance(balance_obj, dict) else None
            if isinstance(total_obj, dict):
                total_equity = total_obj.get("amount")
            elif isinstance(total_obj, (int, float, str)):
                total_equity = total_obj
    except Exception:  # noqa: BLE001
        # Details endpoint can fail without the balance call failing — soft-skip.
        pass

    return {
        "cash": _dec(cash),
        "buying_power": _dec(buying_power),
        "total_equity": _dec(total_equity),
        "currency": currency,
    }


def delete_account(app_user_id: uuid.UUID, user_secret: str, snaptrade_account_id: str) -> None:
    # SnapTrade exposes connection-level disconnect; account removal happens
    # when the underlying connection is removed. Best-effort.
    try:
        _client().connections.remove_brokerage_authorization(
            authorization_id=snaptrade_account_id,
            user_id=str(app_user_id),
            user_secret=user_secret,
        )
    except Exception:
        pass


def place_market_order(
    app_user_id: uuid.UUID,
    user_secret: str,
    snaptrade_account_id: str,
    *,
    symbol: str,
    side: str,           # "BUY" or "SELL"
    quantity: float,
    time_in_force: str = "Day",
) -> dict[str, Any]:
    """Direct placement (skips the impact-check preview). Returns the raw
    SnapTrade order response as a dict."""
    resp = _client().trading.place_force_order(
        user_id=str(app_user_id),
        user_secret=user_secret,
        account_id=snaptrade_account_id,
        action=side,
        order_type="Market",
        price=None,
        stop=None,
        time_in_force=time_in_force,
        units=quantity,
        universal_symbol_id=None,
        symbol=symbol,
    )
    return resp.body if hasattr(resp, "body") else resp


def place_limit_order(
    app_user_id: uuid.UUID,
    user_secret: str,
    snaptrade_account_id: str,
    *,
    symbol: str,
    side: str,
    quantity: float,
    limit_price: float,
    time_in_force: str = "Day",
) -> dict[str, Any]:
    resp = _client().trading.place_force_order(
        user_id=str(app_user_id),
        user_secret=user_secret,
        account_id=snaptrade_account_id,
        action=side,
        order_type="Limit",
        price=limit_price,
        stop=None,
        time_in_force=time_in_force,
        units=quantity,
        universal_symbol_id=None,
        symbol=symbol,
    )
    return resp.body if hasattr(resp, "body") else resp


def get_order(
    app_user_id: uuid.UUID, user_secret: str, snaptrade_account_id: str, broker_order_id: str
) -> dict[str, Any] | None:
    resp = _client().account_information.get_user_account_orders(
        user_id=str(app_user_id),
        user_secret=user_secret,
        account_id=snaptrade_account_id,
    )
    orders = resp.body if hasattr(resp, "body") else resp
    if not orders:
        return None
    for o in orders:
        if str(o.get("brokerage_order_id") or o.get("id")) == broker_order_id:
            return o
    return None
