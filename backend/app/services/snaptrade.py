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


def list_account_activities(
    app_user_id: uuid.UUID,
    user_secret: str,
    snaptrade_account_id: str,
) -> list[dict[str, Any]]:
    """Pull all activities (fills, dividends, fees, etc.) for one account.

    Response is wrapped: {"data": [...], "pagination": {...}}. We unwrap.
    Caller is responsible for filtering down to types they care about
    (typically "BUY" / "SELL" for fills).
    """
    resp = _client().account_information.get_account_activities(
        user_id=str(app_user_id),
        user_secret=user_secret,
        account_id=snaptrade_account_id,
    )
    body: Any = resp.body if hasattr(resp, "body") else resp
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, list):
            return data
    if isinstance(body, list):
        return body
    return []


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


def resolve_universal_symbol_id(
    app_user_id: uuid.UUID, user_secret: str, snaptrade_account_id: str, ticker: str
) -> str | None:
    """Translate a human ticker (e.g. "AAPL") into SnapTrade's universal
    symbol UUID — required by the options-chain endpoint. Returns None if
    the broker doesn't list it.

    We use the per-account search (not the global one) because what's tradeable
    depends on the broker behind the account. Picks the result whose ticker
    exactly matches; falls back to the first hit if there's no exact match.
    """
    resp = _client().reference_data.symbol_search_user_account(
        user_id=str(app_user_id),
        user_secret=user_secret,
        account_id=snaptrade_account_id,
        substring=ticker,
    )
    body: Any = resp.body if hasattr(resp, "body") else resp
    if not isinstance(body, list) or not body:
        return None

    target = ticker.upper()
    # Each hit is shaped like: {"id": "<uuid>", "symbol": "AAPL", ...} or
    # {"id": "<uuid>", "raw_symbol": "AAPL", "universal_symbol": {...}, ...}
    def _get_ticker(hit: dict) -> str | None:
        for k in ("symbol", "raw_symbol", "ticker"):
            v = hit.get(k)
            if isinstance(v, str):
                return v.upper()
        us = hit.get("universal_symbol")
        if isinstance(us, dict):
            return _get_ticker(us)
        return None

    def _get_id(hit: dict) -> str | None:
        v = hit.get("id")
        if isinstance(v, str):
            return v
        us = hit.get("universal_symbol")
        if isinstance(us, dict):
            return _get_id(us)
        return None

    exact = next((h for h in body if isinstance(h, dict) and _get_ticker(h) == target), None)
    chosen = exact or (body[0] if isinstance(body[0], dict) else None)
    return _get_id(chosen) if chosen else None


def get_options_chain(
    app_user_id: uuid.UUID, user_secret: str, snaptrade_account_id: str, symbol: str
) -> list[dict[str, Any]]:
    """Return SnapTrade's raw options chain for `symbol` on this account.

    SnapTrade's chain endpoint requires a *universal symbol UUID*, not the
    ticker string. We look up the UUID first, then fetch the chain.
    Returns [] if the symbol isn't tradable at this broker.

    Shape varies between brokers — most return an array of expiration entries,
    each with strikes. Caller is responsible for extracting whatever it needs.
    """
    universal_id = resolve_universal_symbol_id(
        app_user_id, user_secret, snaptrade_account_id, symbol
    )
    if not universal_id:
        return []

    resp = _client().options.get_options_chain(
        user_id=str(app_user_id),
        user_secret=user_secret,
        account_id=snaptrade_account_id,
        symbol=universal_id,
    )
    body: Any = resp.body if hasattr(resp, "body") else resp
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("chain", "expirations", "options"):
            if key in body and isinstance(body[key], list):
                return body[key]
    return []


def list_option_expiries(
    app_user_id: uuid.UUID, user_secret: str, snaptrade_account_id: str, symbol: str
) -> list[str]:
    """Extract sorted unique expiry dates (YYYY-MM-DD) from the options chain.

    Defensive about the response shape — different brokers via SnapTrade use
    different field names. We look for the obvious ones and skip entries we
    can't parse.
    """
    chain = get_options_chain(app_user_id, user_secret, snaptrade_account_id, symbol)
    seen: set[str] = set()
    for entry in chain:
        if not isinstance(entry, dict):
            continue
        for k in ("expiration_date", "expirationDate", "expiry", "expiry_date", "date"):
            v = entry.get(k)
            if v:
                # Normalize to YYYY-MM-DD (strip time / tz if present)
                if isinstance(v, str):
                    seen.add(v[:10])
                break
    return sorted(seen)


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
