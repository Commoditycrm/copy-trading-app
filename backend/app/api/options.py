"""Options chain lookups — populate the Trade Panel's expiry + strike pickers
from Alpaca's option-contracts endpoint.
"""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter
from app.database import get_db
from app.models.broker_account import BrokerAccount
from app.models.user import User
from app.services.crypto import decrypt_json

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
    return {"symbol": symbol.upper(), "expiry": str(expiry), "right": want_type, "strikes": strikes}
