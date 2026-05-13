"""Options chain lookups — used by the Trade Panel to populate the expiry
dropdown (and later: strike dropdown) once the user picks a symbol.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.database import get_db
from app.models.broker_account import BrokerAccount
from app.models.user import User
from app.services import snaptrade as st

router = APIRouter(prefix="/api/options", tags=["options"])


@router.get("/expiries")
def list_expiries(
    account_id: uuid.UUID = Query(..., description="Local BrokerAccount id"),
    symbol: str = Query(..., min_length=1, max_length=12),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, list[str] | str]:
    """Return all option expiry dates available for `symbol` at the broker
    behind this BrokerAccount. Sorted ascending (soonest first)."""
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "broker_account_not_found")
    if not user.encrypted_snaptrade_user_secret:
        raise HTTPException(409, "snaptrade_not_registered")
    secret = st.decrypt_secret(user.encrypted_snaptrade_user_secret)

    try:
        expiries = st.list_option_expiries(
            user.id, secret, acct.snaptrade_account_id, symbol.upper()
        )
    except Exception as exc:  # noqa: BLE001
        # Surface SnapTrade's message — usually "unknown symbol" or
        # "broker does not support options". Frontend shows it inline.
        raise HTTPException(502, f"snaptrade_error: {exc}")

    return {"symbol": symbol.upper(), "expiries": expiries}
