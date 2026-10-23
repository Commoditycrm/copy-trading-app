"""Admin → Paper auto-trades: QA automatic Alpaca PAPER trading.

Read: the two switches, each listed account's paper checks and order caps,
recent orders on those accounts and their copies, and the guard's refusals.
Write (all audited): the admin toggle — refused unless the environment switch
is on and every listed account passes its checks — a forced identity check
(one read-only Alpaca get_account), and clearing a tripped order cap.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.config import get_settings
from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.broker_account import BrokerAccount
from app.models.discord_message import DiscordMessage
from app.models.order import Order
from app.models.user import User
from app.services import app_settings, audit, paper_autotrade, shared_accounts
from app.services.crypto import decrypt_json

router = APIRouter(prefix="/api/admin/paper-autotrade", tags=["admin"])


def _creds(acct: BrokerAccount) -> dict | None:
    try:
        return decrypt_json(acct.encrypted_credentials)
    except Exception:  # noqa: BLE001
        return None


def _account_view(db: Session, acct: BrokerAccount | None, account_id: uuid.UUID) -> dict[str, Any]:
    if acct is None:
        return {"id": str(account_id), "missing": True, "problems": ["broker account not found"]}
    owner = db.get(User, acct.user_id)
    problems = paper_autotrade.static_problems(acct.broker, acct.is_paper, _creds(acct))
    shared = shared_accounts.shared_with_another_user(db, acct)
    if shared:
        problems.append("shared with another user")
    if acct.connection_status != "connected":
        problems.append(f"not connected ({acct.connection_status})")
    try:
        counts = paper_autotrade.counts(acct.id)
    except Exception:  # noqa: BLE001
        counts = None
    num = acct.broker_account_number
    return {
        "id": str(acct.id),
        "owner": (owner.display_name or "—") if owner else "—",
        "owner_role": owner.role.value if owner else None,
        "broker": acct.broker.value,
        "label": acct.label,
        "is_paper": acct.is_paper,
        "connection_status": acct.connection_status,
        "number_masked": f"{num[:2]}***{num[-3:]}" if num else None,
        "problems": problems,
        "identity": paper_autotrade.last_identity(acct.id),
        "counts": counts,
    }


def _source(o: Order, discord_ids: set) -> str:
    if o.parent_order_id is not None:
        return "copy"
    if o.id in discord_ids:
        return "discord"
    return "panel/exit"


@router.get("")
def status(db: Session = Depends(get_db), _: User = Depends(require_admin)) -> dict:
    s = get_settings()
    ids = sorted(paper_autotrade.allowed_ids(), key=str)
    accounts = {a.id: a for a in db.execute(select(BrokerAccount).where(BrokerAccount.id.in_(ids))).scalars()} if ids else {}
    orders: list[dict] = []
    if ids:
        own = select(Order.id).where(Order.broker_account_id.in_(ids))
        rows = list(db.execute(
            select(Order).where(or_(Order.broker_account_id.in_(ids), Order.parent_order_id.in_(own)))
            .order_by(Order.created_at.desc()).limit(100)
        ).scalars())
        discord_ids = set(db.execute(select(DiscordMessage.order_id).where(
            DiscordMessage.order_id.in_([o.id for o in rows]))).scalars()) if rows else set()
        for o in rows:
            orders.append({
                "id": str(o.id), "ref": str(o.id).replace("-", "")[:8].upper(),
                "source": _source(o, discord_ids),
                "account_id": str(o.broker_account_id) if o.broker_account_id else None,
                "symbol": o.symbol, "side": o.side.value, "quantity": str(o.quantity),
                "order_type": o.order_type.value, "status": o.status.value,
                "reject_reason": o.reject_reason,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            })
    events = [{
        "at": e.created_at.isoformat() if e.created_at else None, "action": e.action,
        "account_id": str(e.entity_id) if e.entity_id else None, "detail": e.metadata_json,
    } for e in db.execute(
        select(AuditLog).where(AuditLog.action.like("paper_autotrade.%"))
        .order_by(AuditLog.created_at.desc()).limit(50)
    ).scalars()]
    return {
        "env_enabled": paper_autotrade.env_enabled(),
        "toggle_on": app_settings.get_override(db, paper_autotrade.TOGGLE_KEY) is True,
        "effective": paper_autotrade.enabled(),
        "limits": {"per_minute": s.qa_paper_autotrade_max_orders_per_minute,
                   "per_day": s.qa_paper_autotrade_max_orders_per_day},
        "accounts": [_account_view(db, accounts.get(i), i) for i in ids],
        "orders": orders,
        "events": events,
    }


class ToggleIn(BaseModel):
    on: bool


@router.post("/toggle")
def toggle(body: ToggleIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    if not paper_autotrade.env_enabled():
        raise HTTPException(403, "QA paper auto-trading isn't enabled on this server.")
    if body.on:
        ids = paper_autotrade.allowed_ids()
        if not ids:
            raise HTTPException(409, "No paper accounts are listed for auto-trading.")
        bad = {}
        for i in ids:
            view = _account_view(db, db.get(BrokerAccount, i), i)
            if view["problems"]:
                bad[str(i)] = view["problems"]
        if bad:
            raise HTTPException(409, {"message": "Some listed accounts can't be auto-traded.", "accounts": bad})
    app_settings.set_flag(db, paper_autotrade.TOGGLE_KEY, body.on)
    audit.record(db, actor_user_id=admin.id, action="paper_autotrade.toggled",
                 metadata={"on": body.on})
    db.commit()
    return {"toggle_on": body.on}


@router.post("/verify/{account_id}")
def verify(account_id: uuid.UUID, db: Session = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    """One read-only get_account call to Alpaca. Places nothing."""
    if account_id not in paper_autotrade.allowed_ids():
        raise HTTPException(404, "That account isn't listed for paper auto-trading.")
    acct = db.get(BrokerAccount, account_id)
    if acct is None:
        raise HTTPException(404, "Broker account not found.")
    creds = _creds(acct)
    problem = None
    if creds is None:
        problem = "credentials can't be read"
    else:
        from app.brokers.alpaca import AlpacaAdapter  # noqa: PLC0415

        probs = paper_autotrade.static_problems(acct.broker, acct.is_paper, creds)
        if probs:
            problem = "; ".join(probs)
        else:
            problem = paper_autotrade.verify_identity(account_id, creds, AlpacaAdapter(creds)._c(), force=True)  # noqa: SLF001
    audit.record(db, actor_user_id=admin.id, action="paper_autotrade.verified",
                 entity_type="broker_account", entity_id=account_id, metadata={"problem": problem})
    db.commit()
    return {"ok": problem is None, "problem": problem, "identity": paper_autotrade.last_identity(account_id)}


@router.post("/reset/{account_id}")
def reset(account_id: uuid.UUID, db: Session = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    paper_autotrade.reset_cap(account_id)
    audit.record(db, actor_user_id=admin.id, action="paper_autotrade.cap_reset",
                 entity_type="broker_account", entity_id=account_id)
    db.commit()
    return {"ok": True}
