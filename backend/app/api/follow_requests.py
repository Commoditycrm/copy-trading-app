"""Subscriber → trader follow-request / approval workflow.

A subscriber can't silently follow a trader anymore: they POST a request,
the trader approves or rejects it, and both sides get an in-app notification
(pushed live via SSE) plus an email. Approval grants PERMISSION only —
settings.follow_trader then lets the subscriber actually start following an
approved trader (see the approval check there).
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, require_subscriber, require_trader
from app.database import get_db
from app.models.follow_request import FollowRequest, FollowRequestStatus
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.schemas.follow import FollowRequestCreate, FollowRequestOut
from app.services import audit, cache, notifications
from app.services.email import send_follow_decision_email, send_follow_request_email

router = APIRouter(prefix="/api/follow-requests", tags=["follow-requests"])


def _subscriber_label(u: User) -> str:
    return (u.display_name or "").strip() or u.email


def _trader_label(u: User) -> str:
    return (u.business_name or "").strip() or (u.display_name or "").strip() or u.email


def _to_out(
    fr: FollowRequest, *, subscriber: User | None = None, trader: User | None = None
) -> FollowRequestOut:
    out = FollowRequestOut(
        id=fr.id,
        subscriber_id=fr.subscriber_id,
        trader_id=fr.trader_id,
        status=fr.status.value,
        decided_at=fr.decided_at,
        created_at=fr.created_at,
    )
    if subscriber is not None:
        out.subscriber_name = subscriber.display_name
        out.subscriber_email = subscriber.email
    if trader is not None:
        out.trader_name = trader.display_name
        out.trader_business_name = trader.business_name
    return out


@router.post("", response_model=FollowRequestOut, status_code=status.HTTP_201_CREATED)
def create_follow_request(
    payload: FollowRequestCreate,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> FollowRequestOut:
    """Request to follow a trader. Re-requesting a previously rejected trader
    re-opens the same row to pending. An already-approved pair is returned
    unchanged (no duplicate notification)."""
    trader = db.get(User, payload.trader_id)
    if not trader or trader.role != UserRole.TRADER or not trader.is_active:
        raise HTTPException(404, "trader_not_found")

    existing = db.execute(
        select(FollowRequest).where(
            FollowRequest.subscriber_id == user.id,
            FollowRequest.trader_id == trader.id,
        )
    ).scalar_one_or_none()

    if existing and existing.status == FollowRequestStatus.APPROVED:
        return _to_out(existing, trader=trader)

    # Auto-allow traders: no approval step — record the pair as approved right
    # away (idempotent) and skip the trader notification. The subscriber then
    # follows directly; settings.follow_trader also permits it via the trader's
    # auto_approve_follows flag.
    ts = db.get(TraderSettings, trader.id)
    if ts and ts.auto_approve_follows:
        if existing:
            existing.status = FollowRequestStatus.APPROVED
            existing.decided_at = datetime.now(timezone.utc)
            fr = existing
        else:
            fr = FollowRequest(
                subscriber_id=user.id, trader_id=trader.id,
                status=FollowRequestStatus.APPROVED,
                decided_at=datetime.now(timezone.utc),
            )
            db.add(fr)
        db.flush()
        audit.record(
            db, actor_user_id=user.id, action="follow.auto_approved",
            entity_type="follow_request", entity_id=fr.id,
            metadata={"trader_id": str(trader.id)},
            ip_address=client_ip(request),
        )
        db.commit()
        db.refresh(fr)
        return _to_out(fr, trader=trader)

    if existing:
        # Pending (idempotent re-ask) or rejected (re-open) → pending.
        existing.status = FollowRequestStatus.PENDING
        existing.decided_at = None
        fr = existing
    else:
        fr = FollowRequest(
            subscriber_id=user.id, trader_id=trader.id,
            status=FollowRequestStatus.PENDING,
        )
        db.add(fr)
    db.flush()  # materialise fr.id for the notification metadata

    sub_label = _subscriber_label(user)
    notifications.create_notification(
        db,
        user_id=trader.id,
        type="follow.requested",
        message=f"{sub_label} requested to follow you.",
        metadata={
            "request_id": str(fr.id),
            "subscriber_id": str(user.id),
            "subscriber_name": sub_label,
        },
    )
    audit.record(
        db, actor_user_id=user.id, action="follow.requested",
        entity_type="follow_request", entity_id=fr.id,
        metadata={"trader_id": str(trader.id)},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(fr)

    # Email is best-effort + off the request path.
    background.add_task(
        send_follow_request_email, trader.email, trader.display_name, sub_label,
    )
    return _to_out(fr, trader=trader)


@router.get("/mine", response_model=list[FollowRequestOut])
def list_my_requests(
    db: Session = Depends(get_db), user: User = Depends(require_subscriber),
) -> list[FollowRequestOut]:
    """A subscriber's own requests (all statuses) — drives the status chips
    and the list of approved traders they may follow."""
    rows = list(db.execute(
        select(FollowRequest, User)
        .join(User, User.id == FollowRequest.trader_id)
        .where(FollowRequest.subscriber_id == user.id)
        .order_by(FollowRequest.created_at.desc())
    ).all())
    return [_to_out(fr, trader=trader) for fr, trader in rows]


@router.get("/incoming", response_model=list[FollowRequestOut])
def list_incoming_requests(
    status_filter: str = Query("pending", alias="status"),
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> list[FollowRequestOut]:
    """Requests addressed to the current trader. Defaults to pending (the
    actionable set); pass ?status=all for the full history."""
    q = (
        select(FollowRequest, User)
        .join(User, User.id == FollowRequest.subscriber_id)
        .where(FollowRequest.trader_id == user.id)
    )
    if status_filter != "all":
        try:
            q = q.where(FollowRequest.status == FollowRequestStatus(status_filter))
        except ValueError:
            raise HTTPException(422, f"invalid status: {status_filter!r}")
    q = q.order_by(FollowRequest.created_at.desc())
    rows = list(db.execute(q).all())
    return [_to_out(fr, subscriber=sub) for fr, sub in rows]


def _decide(
    request_id: uuid.UUID,
    *,
    approve: bool,
    request: Request,
    background: BackgroundTasks,
    db: Session,
    user: User,
) -> FollowRequestOut:
    fr = db.get(FollowRequest, request_id)
    if not fr or fr.trader_id != user.id:
        raise HTTPException(404, "not_found")
    if fr.status != FollowRequestStatus.PENDING:
        raise HTTPException(409, "not_pending")

    fr.status = FollowRequestStatus.APPROVED if approve else FollowRequestStatus.REJECTED
    fr.decided_at = datetime.now(timezone.utc)
    subscriber = db.get(User, fr.subscriber_id)
    trader_label = _trader_label(user)

    # Auto-follow on approval — the subscriber shouldn't need a second click.
    # Point their follow at this trader (single-valued; overwrites any prior
    # follow) and bust the fanout caches so copy_engine sees it immediately.
    if approve:
        ss = db.get(SubscriberSettings, fr.subscriber_id)
        if ss is not None:
            prev_trader = ss.following_trader_id
            ss.following_trader_id = user.id
            if prev_trader and prev_trader != user.id:
                cache.invalidate_subscribers_for_trader(prev_trader)
            cache.invalidate_subscribers_for_trader(user.id)

    notifications.create_notification(
        db,
        user_id=fr.subscriber_id,
        type="follow.approved" if approve else "follow.rejected",
        message=(
            f"{trader_label} approved your request — you're now following them."
            if approve else
            f"{trader_label} declined your follow request."
        ),
        metadata={"request_id": str(fr.id), "trader_id": str(user.id)},
    )
    audit.record(
        db, actor_user_id=user.id,
        action="follow.approved" if approve else "follow.rejected",
        entity_type="follow_request", entity_id=fr.id,
        metadata={"subscriber_id": str(fr.subscriber_id)},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(fr)

    if subscriber is not None:
        background.add_task(
            send_follow_decision_email,
            subscriber.email, subscriber.display_name, trader_label, approve,
        )
    return _to_out(fr, subscriber=subscriber)


@router.post("/{request_id}/approve", response_model=FollowRequestOut)
def approve_follow_request(
    request_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> FollowRequestOut:
    return _decide(request_id, approve=True, request=request,
                   background=background, db=db, user=user)


@router.post("/{request_id}/reject", response_model=FollowRequestOut)
def reject_follow_request(
    request_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> FollowRequestOut:
    return _decide(request_id, approve=False, request=request,
                   background=background, db=db, user=user)


@router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_follow_request(
    request_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> None:
    """Subscriber withdraws their own still-pending request."""
    fr = db.get(FollowRequest, request_id)
    if not fr or fr.subscriber_id != user.id:
        raise HTTPException(404, "not_found")
    if fr.status != FollowRequestStatus.PENDING:
        raise HTTPException(409, "not_pending")
    db.delete(fr)
    db.commit()
