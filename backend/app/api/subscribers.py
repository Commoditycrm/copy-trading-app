"""Trader-only views over their subscribers."""
import uuid
from datetime import date, timedelta, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, require_trader
from app.database import get_db
from app.models.broker_account import BrokerAccount
from app.models.follow_request import FollowRequest
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User
from app.schemas.pagination import Page
from app.schemas.settings import (
    BulkCopyStateOut,
    BulkCopyToggleIn,
    SubscriberBulkRemoveIn,
    SubscriberMultiplierIn,
    SubscriberSummary,
)
from app.services import audit, cache, notifications
from app.services.pnl import realized_pnl_by_day

router = APIRouter(prefix="/api/subscribers", tags=["subscribers"])


@router.get("", response_model=Page[SubscriberSummary])
def list_subscribers(
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
    search: str | None = Query(default=None, description="email / name substring"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[SubscriberSummary]:
    """Server-side paginated subscriber list. The per-subscriber broker count +
    30-day realized P&L (an N+1 that was previously run for EVERY follower) now
    runs only for the page being shown."""
    base = (
        select(User, SubscriberSettings)
        .join(SubscriberSettings, SubscriberSettings.user_id == User.id)
        .where(SubscriberSettings.following_trader_id == trader.id)
    )
    term = (search or "").strip()
    if term:
        like = f"%{term}%"
        base = base.where(or_(User.email.ilike(like), User.display_name.ilike(like)))

    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    rows = db.execute(base.order_by(User.email).limit(limit).offset(offset)).all()

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
    return Page(items=out, total=total, limit=limit, offset=offset)


@router.get("/stats")
def subscriber_stats(
    db: Session = Depends(get_db), trader: User = Depends(require_trader)
) -> dict[str, int]:
    """Cheap header counts (no per-subscriber P&L) — total / copy-active /
    with a connected broker — so the summary reflects EVERY follower, not the
    page shown."""
    where = SubscriberSettings.following_trader_id == trader.id
    total = db.execute(select(func.count()).where(where)).scalar_one()
    active = db.execute(
        select(func.count()).where(where, SubscriberSettings.copy_enabled.is_(True))
    ).scalar_one()
    sub_ids = select(SubscriberSettings.user_id).where(where)
    with_broker = db.execute(
        select(func.count(func.distinct(BrokerAccount.user_id))).where(
            BrokerAccount.user_id.in_(sub_ids)
        )
    ).scalar_one()
    return {"total": total, "active": active, "with_broker": with_broker}


def _bulk_state(db: Session, trader_id) -> BulkCopyStateOut:
    rows = db.execute(
        select(SubscriberSettings.copy_enabled).where(
            SubscriberSettings.following_trader_id == trader_id
        )
    ).all()
    total = len(rows)
    enabled = sum(1 for (e,) in rows if e)
    ts = db.get(TraderSettings, trader_id)
    return BulkCopyStateOut(total=total, enabled=enabled, paused=bool(ts and ts.copy_paused))


@router.get("/copy-state", response_model=BulkCopyStateOut)
def get_bulk_copy_state(
    db: Session = Depends(get_db), trader: User = Depends(require_trader)
) -> BulkCopyStateOut:
    return _bulk_state(db, trader.id)


@router.patch("/copy-state", response_model=BulkCopyStateOut)
def set_bulk_copy_state(
    payload: BulkCopyToggleIn,
    request: Request,
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
) -> BulkCopyStateOut:
    """Master fanout gate. `enabled=false` pauses fanout to all subscribers
    without touching their individual copy_enabled flags. `enabled=true`
    resumes — subscribers' own preferences take over again."""
    ts = db.get(TraderSettings, trader.id)
    if ts is None:
        raise HTTPException(404, "settings_missing")
    ts.copy_paused = not payload.enabled
    audit.record(
        db,
        actor_user_id=trader.id,
        action="trader.copy_paused" if ts.copy_paused else "trader.copy_resumed",
        entity_type="trader_settings",
        entity_id=trader.id,
        metadata={"copy_paused": ts.copy_paused},
        ip_address=client_ip(request),
    )
    db.commit()
    cache.invalidate_subscribers_for_trader(trader.id)
    return _bulk_state(db, trader.id)


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
    cache.invalidate_subscribers_for_trader(trader.id)
    return {"ok": True}


def _unfollow(
    db: Session, s: SubscriberSettings, *, trader: User,
    request: Request, via: str,
) -> None:
    """Set following_trader_id=NULL, flip copy_enabled off, and notify the
    subscriber.

    The subscriber's account, broker connections, multiplier, P&L history
    and any in-flight mirror orders are preserved — this just stops future
    fanout from THIS trader. The subscriber can re-follow at any time from
    their settings page. Existing positions remain owned by the subscriber.

    A notification is created so the subscriber sees a toast immediately
    if their app is open AND a persistent bell-icon entry on next login —
    we don't silently drop them. JWT/session is untouched: there's no
    security reason to log them out, only a need to inform them.
    """
    subscriber_id = s.user_id
    s.following_trader_id = None
    # Belt-and-braces: flip the subscriber-side copy flag too so even if
    # the subscriber re-follows by mistake later, fanout doesn't resume
    # silently — they have to opt in explicitly.
    s.copy_enabled = False
    # Revoke the follow approval. A trader removing a subscriber withdraws
    # permission — the subscriber must send a fresh request (and be approved
    # again) to re-follow, not just click Follow. Deleting the row resets
    # their Traders list to "Request to follow".
    db.execute(
        delete(FollowRequest).where(
            FollowRequest.subscriber_id == subscriber_id,
            FollowRequest.trader_id == trader.id,
        )
    )
    audit.record(
        db,
        actor_user_id=trader.id,
        action="trader.subscriber_removed",
        entity_type="subscriber_settings",
        entity_id=subscriber_id,
        metadata={"subscriber_id": str(subscriber_id), "via": via},
        ip_address=client_ip(request),
    )
    # Notify the subscriber. Persistent + SSE-pushed, so a logged-in
    # browser tab gets a live toast and a closed-tab subscriber sees the
    # bell badge on next login. Wrapped in try/except: a notification
    # delivery failure should not roll back the unfollow itself — the
    # state change is what matters; the notice is a courtesy.
    trader_label = trader.display_name or trader.email
    try:
        notifications.create_notification(
            db,
            user_id=subscriber_id,
            type="trader.unfollowed_you",
            message=(
                f"{trader_label} has removed you from their subscribers. "
                f"You will not receive new copy trades. Please "
                f"manage all existing positions in your account yourself."
            ),
            metadata={
                "trader_id": str(trader.id),
                "trader_email": trader.email,
                "trader_display_name": trader.display_name,
                "via": via,
            },
        )
    except Exception:  # noqa: BLE001
        # Don't fail the unfollow because notification couldn't be sent.
        # Audit row above is the source of truth for the action.
        pass


@router.delete("/{subscriber_id}")
def remove_subscriber(
    subscriber_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
) -> dict:
    """Remove (unfollow) a single subscriber. Preserves the subscriber's
    account and history — only the follow relationship is broken."""
    s = db.get(SubscriberSettings, subscriber_id)
    if not s or s.following_trader_id != trader.id:
        raise HTTPException(404, "subscriber_not_found")
    _unfollow(db, s, trader=trader, request=request, via="single")
    db.commit()
    cache.invalidate_subscribers_for_trader(trader.id)
    return {"ok": True, "removed": 1}


@router.post("/bulk-remove")
def bulk_remove_subscribers(
    payload: SubscriberBulkRemoveIn,
    request: Request,
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
) -> dict:
    """Bulk version of DELETE /api/subscribers/{id}.

    IDs that don't belong to this trader (already-unfollowed, wrong
    trader, or just garbage) are silently skipped — partial-success is
    the right ergonomic for a multi-select UI where the user clicked
    rows that may have shifted on the server in the meantime.
    """
    rows = db.execute(
        select(SubscriberSettings).where(
            SubscriberSettings.user_id.in_(payload.subscriber_ids),
            SubscriberSettings.following_trader_id == trader.id,
        )
    ).scalars().all()
    for s in rows:
        _unfollow(db, s, trader=trader, request=request, via="bulk")
    if rows:
        db.commit()
        cache.invalidate_subscribers_for_trader(trader.id)
    return {"ok": True, "removed": len(rows)}
