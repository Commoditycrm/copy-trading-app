"""Broker connection endpoints (direct broker integration).

Flow:
  1. POST /api/brokers
       User pastes API key/secret (plus paper flag for Alpaca). We verify the
       connection with the broker before storing — bad credentials are rejected
       immediately rather than failing silently later.
  2. GET /api/brokers
       List my connected accounts.
  3. POST /api/brokers/{id}/refresh-balance
       Pull cash/buying_power/equity from the broker into our cached snapshot.
  4. DELETE /api/brokers/{id}
       Remove the connection.
"""
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user
from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter
from app.database import get_db
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.user import User
from app.schemas.broker import BrokerAccountOut, ConnectBrokerIn
from app.services import audit
from app.services.crypto import decrypt_json, encrypt_json

router = APIRouter(prefix="/api/brokers", tags=["brokers"])


def _credentials_for(payload: ConnectBrokerIn) -> dict[str, Any]:
    match payload.broker:
        case BrokerName.ALPACA:
            if not payload.alpaca:
                raise HTTPException(422, "alpaca credentials required")
            return payload.alpaca.model_dump()
    raise HTTPException(422, "unknown broker")


def _refresh_balance_into(acct: BrokerAccount, creds: dict[str, Any]) -> None:
    """Best-effort. Errors are recorded into last_error, not raised."""
    try:
        adapter = adapter_for(acct, creds)
        if isinstance(adapter, AlpacaAdapter):
            bal = adapter.get_balance_snapshot()
            acct.cash = bal["cash"]
            acct.buying_power = bal["buying_power"]
            acct.total_equity = bal["total_equity"]
            acct.currency = bal["currency"]
            acct.balance_updated_at = datetime.now(timezone.utc)
    except Exception as exc:  # noqa: BLE001
        acct.last_error = f"balance fetch failed: {str(exc)[:400]}"


@router.post("", response_model=BrokerAccountOut, status_code=status.HTTP_201_CREATED)
def connect(
    payload: ConnectBrokerIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BrokerAccount:
    creds = _credentials_for(payload)

    # Build an unsaved row so we can run verify_connection() against it. Don't
    # persist anything if the broker rejects the credentials — that prevents
    # ghost rows from cluttering the UI.
    acct = BrokerAccount(
        user_id=user.id,
        broker=payload.broker,
        label=payload.label,
        is_paper=bool(creds.get("paper", True)),
        supports_fractional=True,
        encrypted_credentials=encrypt_json(creds),
        connection_status="pending",
    )

    try:
        info = adapter_for(acct, creds).verify_connection()
        acct.broker_account_number = info.broker_account_id
        acct.supports_fractional = info.supports_fractional
        acct.connection_status = "connected"
        # Pull balance immediately so the UI doesn't have a blank row.
        _refresh_balance_into(acct, creds)
    except Exception as exc:  # noqa: BLE001
        audit.record(
            db, actor_user_id=user.id, action="broker.connect_failed",
            metadata={"broker": payload.broker.value, "error": str(exc)[:480]},
            ip_address=client_ip(request),
        )
        db.commit()
        raise HTTPException(400, f"broker_error: {exc}")

    db.add(acct)
    db.flush()
    audit.record(
        db, actor_user_id=user.id, action="broker.connected",
        entity_type="broker_account", entity_id=acct.id,
        metadata={"broker": payload.broker.value, "label": payload.label,
                  "is_paper": acct.is_paper, "account": acct.broker_account_number},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(acct)
    return acct


@router.get("", response_model=list[BrokerAccountOut])
def list_my_brokers(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[BrokerAccount]:
    return list(db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == user.id)
        .order_by(BrokerAccount.created_at.desc())
    ).scalars())


@router.post("/{account_id}/refresh-balance", response_model=BrokerAccountOut)
def refresh_balance(
    account_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BrokerAccount:
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "not_found")
    creds = decrypt_json(acct.encrypted_credentials)
    _refresh_balance_into(acct, creds)
    audit.record(
        db, actor_user_id=user.id, action="broker.balance_refreshed",
        entity_type="broker_account", entity_id=acct.id,
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(acct)
    return acct


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_broker(
    account_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "not_found")
    audit.record(
        db, actor_user_id=user.id, action="broker.deleted",
        entity_type="broker_account", entity_id=acct.id,
        metadata={"broker": acct.broker.value, "label": acct.label},
        ip_address=client_ip(request),
    )
    db.delete(acct)
    db.commit()
