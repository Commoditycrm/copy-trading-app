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
