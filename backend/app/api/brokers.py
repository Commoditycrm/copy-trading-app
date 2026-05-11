"""Broker connection endpoints (SnapTrade-backed).

Flow:
  1. POST /api/brokers/portal-url
       Lazily registers the user with SnapTrade if needed, then returns a
       one-time Connection Portal URL. Frontend opens it in a new tab.
  2. User picks brokerage at SnapTrade, authenticates, gets redirected back.
  3. POST /api/brokers/sync
       Pulls the user's current SnapTrade accounts, upserts BrokerAccount rows,
       and removes any that no longer exist on the SnapTrade side.
  4. GET /api/brokers
       Lists current rows.
  5. DELETE /api/brokers/{id}
       Removes the brokerage authorization at SnapTrade and deletes the row.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user
from app.config import get_settings
from app.database import get_db
from app.models.broker_account import BrokerAccount
from app.models.user import User
from app.schemas.broker import BrokerAccountOut, PortalUrlOut, SyncResultOut
from app.services import audit, snaptrade as st

router = APIRouter(prefix="/api/brokers", tags=["brokers"])


def _ensure_snaptrade_user(db: Session, user: User) -> str:
    """Return the decrypted SnapTrade userSecret for this app user, registering
    them with SnapTrade on first call."""
    if user.snaptrade_registered and user.encrypted_snaptrade_user_secret:
        return st.decrypt_secret(user.encrypted_snaptrade_user_secret)
    try:
        identity = st.register_user(user.id)
    except Exception as exc:  # noqa: BLE001
        # Surface SnapTrade's actual error message instead of a 500. The most
        # common cause is plan-tier limits (e.g. Personal keys reject the 2nd
        # user with code 1012).
        msg = str(exc)
        body = getattr(exc, "body", None)
        if body:
            msg = str(body)
        raise HTTPException(502, f"snaptrade_register_failed: {msg}")
    user.encrypted_snaptrade_user_secret = st.encrypt_secret(identity.user_secret)
    user.snaptrade_registered = True
    db.flush()
    return identity.user_secret


@router.post("/portal-url", response_model=PortalUrlOut)
def get_portal_url(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> PortalUrlOut:
    secret = _ensure_snaptrade_user(db, user)
    return_url = f"{get_settings().frontend_base_url}/brokers?snaptrade=connected"
    try:
        url = st.login_redirect_uri(user.id, secret, return_url)
    except Exception as exc:  # noqa: BLE001
        audit.record(
            db,
            actor_user_id=user.id,
            action="broker.portal_url_failed",
            metadata={"error": str(exc)[:480]},
            ip_address=client_ip(request),
        )
        db.commit()
        raise HTTPException(502, f"snaptrade_error: {exc}")

    audit.record(
        db,
        actor_user_id=user.id,
        action="broker.portal_url_issued",
        ip_address=client_ip(request),
    )
    db.commit()
    return PortalUrlOut(redirect_uri=url)


@router.post("/sync", response_model=SyncResultOut)
def sync_accounts(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> SyncResultOut:
    if not user.snaptrade_registered or not user.encrypted_snaptrade_user_secret:
        # Nothing to sync from yet — the user hasn't started a portal session.
        return SyncResultOut(added=0, removed=0, accounts=[])

    secret = st.decrypt_secret(user.encrypted_snaptrade_user_secret)
    try:
        remote = st.list_accounts(user.id, secret)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"snaptrade_error: {exc}")

    existing: dict[str, BrokerAccount] = {
        a.snaptrade_account_id: a
        for a in db.execute(
            select(BrokerAccount).where(BrokerAccount.user_id == user.id)
        ).scalars()
    }
    remote_ids = set()
    added = 0
    for r in remote:
        sid = str(r.get("id"))
        if not sid:
            continue
        remote_ids.add(sid)
        meta = r.get("meta") or {}
        institution = (r.get("institution_name") or r.get("brokerage", {}).get("name") or "BROKER")
        label = r.get("name") or institution
        broker_number = r.get("number")
        is_paper = bool(meta.get("type", "").lower() == "paper") if meta else False
        supports_fractional = bool(meta.get("supports_fractional_units", False))

        if sid in existing:
            row = existing[sid]
            row.broker = institution
            row.label = label
            row.broker_account_number = broker_number
            row.is_paper = is_paper
            row.supports_fractional = supports_fractional
            row.connection_status = "connected"
            row.last_error = None
        else:
            row = BrokerAccount(
                user_id=user.id,
                broker=institution,
                label=label,
                snaptrade_account_id=sid,
                broker_account_number=broker_number,
                is_paper=is_paper,
                supports_fractional=supports_fractional,
                connection_status="connected",
            )
            db.add(row)
            added += 1

    removed = 0
    for sid, row in existing.items():
        if sid not in remote_ids:
            db.delete(row)
            removed += 1

    audit.record(
        db,
        actor_user_id=user.id,
        action="broker.sync",
        metadata={"added": added, "removed": removed, "total": len(remote_ids)},
        ip_address=client_ip(request),
    )
    db.commit()

    fresh = list(
        db.execute(
            select(BrokerAccount).where(BrokerAccount.user_id == user.id).order_by(
                BrokerAccount.created_at.desc()
            )
        ).scalars()
    )
    return SyncResultOut(added=added, removed=removed, accounts=fresh)


@router.get("", response_model=list[BrokerAccountOut])
def list_my_brokers(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[BrokerAccount]:
    return list(
        db.execute(
            select(BrokerAccount).where(BrokerAccount.user_id == user.id).order_by(
                BrokerAccount.created_at.desc()
            )
        ).scalars()
    )


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
    if user.encrypted_snaptrade_user_secret:
        secret = st.decrypt_secret(user.encrypted_snaptrade_user_secret)
        st.delete_account(user.id, secret, acct.snaptrade_account_id)
    audit.record(
        db,
        actor_user_id=user.id,
        action="broker.deleted",
        entity_type="broker_account",
        entity_id=acct.id,
        metadata={"snaptrade_account_id": acct.snaptrade_account_id},
        ip_address=client_ip(request),
    )
    db.delete(acct)
    db.commit()
