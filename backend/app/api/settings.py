from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user, require_subscriber, require_trader
from app.database import get_db
from app.models.settings import RetryInterval, SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.schemas.settings import (
    DailyLossLimitIn,
    DailyLossLimitPctIn,
    FollowTraderIn,
    MaxDrawdownPctIn,
    PerTradeLossLimitPctIn,
    RetryIntervalIn,
    SubscriberSelfMultiplierIn,
    SubscriberSettingsOut,
    SubscriberToggleIn,
    TraderSettingsOut,
    TraderToggleIn,
)
from app.services.pnl import get_account_equity, today_realized_pnl
from app.services import audit, cache

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _settings_out(s: SubscriberSettings, db: Session, include_live: bool = False) -> SubscriberSettingsOut:
    """Build a SubscriberSettingsOut from the ORM row. Pass include_live=True
    on GET requests to populate todays_realized_pnl and account_equity."""
    return SubscriberSettingsOut(
        user_id=s.user_id,
        following_trader_id=s.following_trader_id,
        copy_enabled=s.copy_enabled,
        multiplier=s.multiplier,
        daily_loss_limit=s.daily_loss_limit,
        daily_loss_limit_pct=s.daily_loss_limit_pct,
        per_trade_loss_limit_pct=s.per_trade_loss_limit_pct,
        max_drawdown_pct=s.max_drawdown_pct,
        max_drawdown_equity_baseline=s.max_drawdown_equity_baseline,
        todays_realized_pnl=today_realized_pnl(db, s.user_id) if include_live else None,
        account_equity=get_account_equity(db, s.user_id) if include_live else None,
        retry_interval_open=s.retry_interval_open.value,
        retry_interval_close=s.retry_interval_close.value,
    )


@router.get("/subscriber", response_model=SubscriberSettingsOut)
def get_subscriber_settings(
    db: Session = Depends(get_db), user: User = Depends(require_subscriber)
) -> SubscriberSettingsOut:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    return _settings_out(s, db, include_live=True)


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
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
    return _settings_out(s, db, include_live=True)


@router.patch("/subscriber/daily-loss-limit-pct", response_model=SubscriberSettingsOut)
def set_daily_loss_limit_pct(
    payload: DailyLossLimitPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Set (or clear) the daily loss limit as a % of account equity."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.daily_loss_limit_pct
    s.daily_loss_limit_pct = payload.daily_loss_limit_pct
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.daily_loss_limit_pct_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old": str(old) if old is not None else None,
            "new": str(payload.daily_loss_limit_pct) if payload.daily_loss_limit_pct is not None else None,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
    return _settings_out(s, db, include_live=True)


@router.patch("/subscriber/per-trade-loss-limit", response_model=SubscriberSettingsOut)
def set_per_trade_loss_limit(
    payload: PerTradeLossLimitPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Set (or clear) the per-trade loss limit as a % of account equity."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.per_trade_loss_limit_pct
    s.per_trade_loss_limit_pct = payload.per_trade_loss_limit_pct
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.per_trade_loss_limit_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old": str(old) if old is not None else None,
            "new": str(payload.per_trade_loss_limit_pct) if payload.per_trade_loss_limit_pct is not None else None,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
    return _settings_out(s, db, include_live=True)


@router.patch("/subscriber/max-drawdown", response_model=SubscriberSettingsOut)
def set_max_drawdown(
    payload: MaxDrawdownPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Set (or clear) the max drawdown protection. When enabled, the current
    account equity is captured as the baseline against which drawdown is measured."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old_pct = s.max_drawdown_pct
    s.max_drawdown_pct = payload.max_drawdown_pct
    # Capture equity baseline when protection is first enabled (or re-enabled).
    if payload.max_drawdown_pct is not None:
        equity = get_account_equity(db, user.id)
        s.max_drawdown_equity_baseline = equity
    else:
        s.max_drawdown_equity_baseline = None
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.max_drawdown_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old": str(old_pct) if old_pct is not None else None,
            "new": str(payload.max_drawdown_pct) if payload.max_drawdown_pct is not None else None,
            "equity_baseline": str(s.max_drawdown_equity_baseline) if s.max_drawdown_equity_baseline else None,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
    return _settings_out(s, db, include_live=True)


@router.patch("/subscriber/retry-interval", response_model=SubscriberSettingsOut)
def set_retry_interval(
    payload: RetryIntervalIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Update the subscriber's retry intervals (open and/or close). Either
    field may be omitted — only the supplied ones change. Invalid enum
    values return 422 so the frontend's dropdown stays the source of
    truth for valid options."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")

    def _parse(value: str) -> RetryInterval:
        try:
            return RetryInterval(value)
        except ValueError:
            raise HTTPException(422, f"invalid retry_interval: {value!r}")

    changes: dict[str, str] = {}
    if payload.retry_interval_open is not None:
        new_open = _parse(payload.retry_interval_open)
        if new_open != s.retry_interval_open:
            changes["retry_interval_open"] = new_open.value
            s.retry_interval_open = new_open
    if payload.retry_interval_close is not None:
        new_close = _parse(payload.retry_interval_close)
        if new_close != s.retry_interval_close:
            changes["retry_interval_close"] = new_close.value
            s.retry_interval_close = new_close

    if changes:
        audit.record(
            db,
            actor_user_id=user.id,
            action="subscriber.retry_interval_changed",
            entity_type="subscriber_settings",
            entity_id=user.id,
            metadata=changes,
            ip_address=client_ip(request),
        )
    db.commit()
    db.refresh(s)
    return _settings_out(s, db, include_live=True)


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
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
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
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
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
    old_trader_id = s.following_trader_id
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
    if old_trader_id:
        cache.invalidate_subscribers_for_trader(old_trader_id)
    if payload.trader_id:
        cache.invalidate_subscribers_for_trader(payload.trader_id)
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
