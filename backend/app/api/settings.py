from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user, require_subscriber, require_trader
from app.database import get_db
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.schemas.settings import (
    DailyLossLimitIn,
    FollowTraderIn,
    SubscriberSelfMultiplierIn,
    SubscriberSettingsOut,
    SubscriberToggleIn,
    TraderSettingsOut,
    TraderToggleIn,
)
from app.services.pnl import today_realized_pnl
from app.services import audit

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/subscriber", response_model=SubscriberSettingsOut)
def get_subscriber_settings(
    db: Session = Depends(get_db), user: User = Depends(require_subscriber)
) -> SubscriberSettingsOut:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    return SubscriberSettingsOut(
        user_id=s.user_id,
        following_trader_id=s.following_trader_id,
        copy_enabled=s.copy_enabled,
        multiplier=s.multiplier,
        daily_loss_limit=s.daily_loss_limit,
        todays_realized_pnl=today_realized_pnl(db, user.id),
    )


@router.patch("/subscriber/daily-loss-limit", response_model=SubscriberSettingsOut)
def set_daily_loss_limit(
    payload: DailyLossLimitIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.daily_loss_limit
    s.daily_loss_limit = payload.daily_loss_limit
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.daily_loss_limit_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old": str(old) if old is not None else None,
            "new": str(payload.daily_loss_limit) if payload.daily_loss_limit is not None else None,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    return SubscriberSettingsOut(
        user_id=s.user_id,
        following_trader_id=s.following_trader_id,
        copy_enabled=s.copy_enabled,
        multiplier=s.multiplier,
        daily_loss_limit=s.daily_loss_limit,
        todays_realized_pnl=today_realized_pnl(db, user.id),
    )


@router.patch("/subscriber/copy", response_model=SubscriberSettingsOut)
def toggle_copy(
    payload: SubscriberToggleIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettings:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    s.copy_enabled = payload.copy_enabled
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.copy_toggled",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={"copy_enabled": payload.copy_enabled},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    return s


@router.patch("/subscriber/multiplier", response_model=SubscriberSettingsOut)
def set_own_multiplier(
    payload: SubscriberSelfMultiplierIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettings:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = str(s.multiplier)
    s.multiplier = payload.multiplier
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.multiplier_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={"old": old, "new": str(payload.multiplier)},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    return s


@router.patch("/subscriber/follow", response_model=SubscriberSettingsOut)
def follow_trader(
    payload: FollowTraderIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettings:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    if payload.trader_id is not None:
        trader = db.get(User, payload.trader_id)
        if not trader or trader.role != UserRole.TRADER:
            raise HTTPException(404, "trader_not_found")
    s.following_trader_id = payload.trader_id
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.follow_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={"trader_id": str(payload.trader_id) if payload.trader_id else None},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    return s


@router.get("/trader", response_model=TraderSettingsOut)
def get_trader_settings(
    db: Session = Depends(get_db), user: User = Depends(require_trader)
) -> TraderSettings:
    s = db.get(TraderSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    return s


@router.patch("/trader", response_model=TraderSettingsOut)
def toggle_trading(
    payload: TraderToggleIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> TraderSettings:
    s = db.get(TraderSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    s.trading_enabled = payload.trading_enabled
    audit.record(
        db,
        actor_user_id=user.id,
        action="trader.trading_toggled",
        entity_type="trader_settings",
        entity_id=user.id,
        metadata={"trading_enabled": payload.trading_enabled},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    return s


@router.get("/traders", response_model=list[dict])
def list_available_traders(db: Session = Depends(get_db), _: User = Depends(current_user)) -> list[dict]:
    """Subscribers use this to find the trader to follow."""
    rows = db.execute(select(User).where(User.role == UserRole.TRADER, User.is_active.is_(True))).scalars()
    return [{"id": str(t.id), "display_name": t.display_name, "email": t.email} for t in rows]
