"""Options chain lookups — populate the Trade Panel's expiry + strike pickers
from Alpaca's option-contracts endpoint, plus a per-contract quote so the
panel can auto-fill the Limit price with the ask.
"""
import logging
import uuid
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter, build_occ_symbol
from app.database import get_db
from app.models.broker_account import BrokerAccount
from app.models.user import User
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/options", tags=["options"])


@router.get("/expiries")
def list_expiries(
    account_id: uuid.UUID = Query(..., description="Local BrokerAccount id"),
    symbol: str = Query(..., min_length=1, max_length=12),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Return sorted unique expiry dates for `symbol`'s option chain."""
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "broker_account_not_found")

    creds = decrypt_json(acct.encrypted_credentials)
    adapter = adapter_for(acct, creds)
    if not isinstance(adapter, AlpacaAdapter):
        raise HTTPException(501, "options chain only implemented for alpaca")

    # Default window: today → +180 days (covers near-dated weekly + monthly chains).
    today = date.today()
    try:
        contracts = adapter.list_option_contracts(
            underlying=symbol,
            expiry_gte=today,
            expiry_lte=today + timedelta(days=180),
            limit=10000,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"broker_error: {exc}")

    expiries = sorted({
        str(c.expiration_date) for c in contracts if getattr(c, "expiration_date", None)
    })
    return {"symbol": symbol.upper(), "expiries": expiries}


@router.get("/strikes")
def list_strikes(
    account_id: uuid.UUID = Query(..., description="Local BrokerAccount id"),
    symbol: str = Query(..., min_length=1, max_length=12),
    expiry: date = Query(..., description="Specific expiry date (YYYY-MM-DD)"),
    right: str = Query("call", pattern="^(call|put)$"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Return sorted unique strike prices for one expiry + right (call/put)."""
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "broker_account_not_found")

    creds = decrypt_json(acct.encrypted_credentials)
    adapter = adapter_for(acct, creds)
    if not isinstance(adapter, AlpacaAdapter):
        raise HTTPException(501, "options chain only implemented for alpaca")

    try:
        contracts = adapter.list_option_contracts(
            underlying=symbol, expiry=expiry, limit=10000,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"broker_error: {exc}")

    want_type = right.lower()
    strikes = sorted({
        float(c.strike_price) for c in contracts
        if getattr(c, "strike_price", None) is not None
        and str(getattr(c, "type", "")).lower().endswith(want_type)
    })

    # Underlying price so the UI can default to the nearest-to-ATM strike
    # instead of the chain median (which can be very off for skewed chains).
    # Best-effort: any failure returns None and the UI falls back to median.
    underlying_price: float | None = None
    try:
        px = adapter.get_stock_latest_price(symbol)
        if px is not None and px > 0:
            underlying_price = float(px)
    except Exception:  # noqa: BLE001
        underlying_price = None

    return {
        "symbol": symbol.upper(),
        "expiry": str(expiry),
        "right": want_type,
        "strikes": strikes,
        "underlying_price": underlying_price,
    }


@router.get("/quote")
def get_option_quote(
    account_id: uuid.UUID = Query(..., description="Local BrokerAccount id"),
    symbol: str = Query(..., min_length=1, max_length=12),
    expiry: date = Query(..., description="Specific expiry date (YYYY-MM-DD)"),
    strike: Decimal = Query(..., gt=0, description="Strike price"),
    right: str = Query("call", pattern="^(call|put)$"),
    debug: int = Query(0, description="When 1, include a _debug field with broker + error trace. For troubleshooting only — do NOT rely on this field in the UI."),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Return the latest bid + ask for a specific option contract.

    Used by the trade panel to surface live pricing under the strike picker
    and to default the Limit price field to the current ask (the standard
    "I want to buy at the offer" baseline). Either side may be null when
    the broker returns no quote (illiquid contracts, after-hours, etc.);
    in that case the panel just leaves the limit field empty for manual
    entry."""
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "broker_account_not_found")

    occ: str | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    debug_info: dict = {
        "broker": acct.broker.value,
        "connection_status": acct.connection_status,
        "adapter_class": None,
        "has_quote_method": False,
        "quote_call_returned": None,
        "error_type": None,
        "error_message": None,
        "error_traceback": None,
    }

    try:
        creds = decrypt_json(acct.encrypted_credentials)
        adapter = adapter_for(acct, creds)
        debug_info["adapter_class"] = adapter.__class__.__name__
        occ = build_occ_symbol(symbol, expiry, strike, right)
        has_method = hasattr(adapter, "get_option_latest_quote")
        debug_info["has_quote_method"] = has_method
        if has_method:
            bid, ask = adapter.get_option_latest_quote(occ)
            debug_info["quote_call_returned"] = {
                "bid": str(bid) if bid is not None else None,
                "ask": str(ask) if ask is not None else None,
            }
        else:
            log.info(
                "options/quote: %s broker has no quote method", acct.broker.value
            )
    except Exception as exc:  # noqa: BLE001
        import traceback  # noqa: PLC0415
        log.exception("options/quote: lookup failed for %s/%s: %s", symbol, occ, exc)
        debug_info["error_type"] = exc.__class__.__name__
        debug_info["error_message"] = str(exc)[:500]
        debug_info["error_traceback"] = traceback.format_exc()[-1500:]

    # Mid is a convenience for "fair value" displays; only compute when
    # both sides are present so we don't return a half-mid that the UI
    # might mistake for a real quote.
    mid: Decimal | None = None
    if bid is not None and ask is not None:
        mid = (bid + ask) / Decimal(2)

    out: dict = {
        "symbol": symbol.upper(),
        "occ": occ,
        "expiry": str(expiry),
        "strike": str(strike),
        "right": right.lower(),
        "bid": float(bid) if bid is not None else None,
        "ask": float(ask) if ask is not None else None,
        "mid": float(mid) if mid is not None else None,
    }
    # Inspector-mode field. Only attached when the caller passes
    # ?debug=1 — keeps regular UI responses clean. Useful for
    # diagnosing why null bid/ask is coming back without needing
    # access to the server log stream.
    if debug:
        out["_debug"] = debug_info
    return out
