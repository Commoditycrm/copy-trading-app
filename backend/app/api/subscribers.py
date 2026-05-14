"""Trader-only views over their subscribers."""
import uuid
from datetime import date, timedelta, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, require_trader
from app.database import get_db
from app.models.broker_account import BrokerAccount
from app.models.settings import SubscriberSettings
from app.models.user import User
from app.schemas.settings import (
    BulkCopyStateOut,
    BulkCopyToggleIn,
    SubscriberMultiplierIn,
    SubscriberSummary,
)
from app.services import audit
from app.services.pnl import realized_pnl_by_day

router = APIRouter(prefix="/api/subscribers", tags=["subscribers"])


@router.get("", response_model=list[SubscriberSummary])
def list_subscribers(
    db: Session = Depends(get_db), trader: User = Depends(require_trader)
) -> list[SubscriberSummary]:
    rows = db.execute(
        select(User, SubscriberSettings)
        .join(SubscriberSettings, SubscriberSettings.user_id == User.id)
        .where(SubscriberSettings.following_trader_id == trader.id)
        .order_by(User.email)
    ).all()

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=30)
    out: list[SubscriberSummary] = []
    for u, s in rows:
        broker_count = db.execute(
            select(func.count(BrokerAccount.id)).where(BrokerAccount.user_id == u.id)
        ).scalar_one()
        daily = realized_pnl_by_day(db, u.id, start=start, end=today)
        pnl_30d = sum((p for p, _ in daily.values()), Decimal(0))
        out.append(
            SubscriberSummary(
                user_id=u.id,
                email=u.email,
                display_name=u.display_name,
                copy_enabled=s.copy_enabled,
                multiplier=s.multiplier,
                broker_count=broker_count,
                realized_pnl_30d=pnl_30d,
            )
        )
    return out


@router.get("/copy-state", response_model=BulkCopyStateOut)
def get_bulk_copy_state(
    db: Session = Depends(get_db), trader: User = Depends(require_trader)
) -> BulkCopyStateOut:
    """Aggregate copy_enabled count across all subscribers following this trader."""
    rows = db.execute(
        select(SubscriberSettings.copy_enabled).where(
            SubscriberSettings.following_trader_id == trader.id
        )
    ).all()
    total = len(rows)
    enabled = sum(1 for (e,) in rows if e)
    return BulkCopyStateOut(total=total, enabled=enabled)


@router.patch("/copy-state", response_model=BulkCopyStateOut)
def set_bulk_copy_state(
    payload: BulkCopyToggleIn,
    request: Request,
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
) -> BulkCopyStateOut:
    """Flip copy_enabled for every subscriber currently following this trader.
    Useful as a master kill switch the trader controls from the sidebar."""
    subs = db.execute(
        select(SubscriberSettings).where(
            SubscriberSettings.following_trader_id == trader.id
        )
    ).scalars().all()
    changed = 0
    for s in subs:
        if s.copy_enabled != payload.enabled:
            s.copy_enabled = payload.enabled
            changed += 1
    audit.record(
        db,
        actor_user_id=trader.id,
        action="trader.bulk_copy_toggle",
        entity_type="trader",
        entity_id=trader.id,
        metadata={
            "target_state": str(payload.enabled),
            "subscribers_total": len(subs),
            "subscribers_changed": changed,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    total = len(subs)
    enabled = sum(1 for s in subs if s.copy_enabled)
    return BulkCopyStateOut(total=total, enabled=enabled)


@router.patch("/{subscriber_id}/multiplier")
def set_multiplier(
    subscriber_id: uuid.UUID,
    payload: SubscriberMultiplierIn,
    request: Request,
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
) -> dict:
    s = db.get(SubscriberSettings, subscriber_id)
    if not s or s.following_trader_id != trader.id:
        raise HTTPException(404, "subscriber_not_found")
    old_multiplier = str(s.multiplier)
    s.multiplier = payload.multiplier
    audit.record(
        db,
        actor_user_id=trader.id,
        action="trader.subscriber_multiplier_changed",
        entity_type="subscriber_settings",
        entity_id=subscriber_id,
        metadata={
            "old_multiplier": old_multiplier,
            "new_multiplier": str(payload.multiplier),
        },
        ip_address=client_ip(request),
    )
    db.commit()
    return {"ok": True}
