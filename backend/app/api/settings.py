from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user, require_subscriber, require_trader
from app.database import get_db
from app.models.settings import RetryInterval, SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.schemas.settings import (
    AutoLiquidationLimitIn,
    DailyLossLimitIn,
    DailyLossLimitPctIn,
    DailyProfitLimitIn,
    DailyProfitLimitPctIn,
    FollowTraderIn,
    MaxAccountPctIn,
    MaxPerContractIn,
    RetryIntervalIn,
    SubscriberSelfMultiplierIn,
    SubscriberSettingsOut,
    SubscriberToggleIn,
    SymbolFilterIn,
    TraderSettingsOut,
    TraderToggleIn,
)
from app.services.pnl import today_realized_pnl
from app.services import audit, cache

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _to_out(db: Session, s: SubscriberSettings) -> SubscriberSettingsOut:
    """Build the response payload from a SubscriberSettings ORM row.

    Centralised so adding a new column doesn't require touching every
    PATCH endpoint that returns this shape. ``todays_realized_pnl`` is
    computed from the fills table here — the live tick from pnl_poller
    pushes the same number via SSE for the panel's live refresh."""
    # The followed trader's business_name is surfaced to subscribers so
    # the AppShell can show the trader's brand instead of the default
    # "ARK" wordmark. Cheap one-row lookup via PK — no join needed since
    # we already have the FK.
    trader_business_name: str | None = None
    if s.following_trader_id:
        trader = db.get(User, s.following_trader_id)
        if trader is not None:
            trader_business_name = trader.business_name
    return SubscriberSettingsOut(
        user_id=s.user_id,
        following_trader_id=s.following_trader_id,
        following_trader_business_name=trader_business_name,
        copy_enabled=s.copy_enabled,
        multiplier=s.multiplier,
        daily_loss_limit=s.daily_loss_limit,
        daily_profit_limit=s.daily_profit_limit,
        todays_realized_pnl=today_realized_pnl(db, s.user_id),
        retry_interval_open=s.retry_interval_open.value,
        retry_interval_close=s.retry_interval_close.value,
        symbol_exclusion_list=list(s.symbol_exclusion_list or []),
        symbol_inclusion_list=list(s.symbol_inclusion_list or []),
        max_per_contract=s.max_per_contract,
        max_account_pct_per_day=s.max_account_pct_per_day,
        auto_liquidation_limit=s.auto_liquidation_limit,
        auto_liquidated_at=s.auto_liquidated_at,
        daily_loss_limit_pct=s.daily_loss_limit_pct,
        daily_profit_limit_pct=s.daily_profit_limit_pct,
    )


@router.get("/subscriber", response_model=SubscriberSettingsOut)
def get_subscriber_settings(
    db: Session = Depends(get_db), user: User = Depends(require_subscriber)
) -> SubscriberSettingsOut:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    return _to_out(db, s)


@router.patch("/subscriber/daily-profit-limit", response_model=SubscriberSettingsOut)
def set_daily_profit_limit(
    payload: DailyProfitLimitIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Set / clear the daily realized-profit auto-pause. Symmetric to
    set_daily_loss_limit — same audit + cache-bust + response shape."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.daily_profit_limit
    s.daily_profit_limit = payload.daily_profit_limit
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.daily_profit_limit_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old": str(old) if old is not None else None,
            "new": str(payload.daily_profit_limit) if payload.daily_profit_limit is not None else None,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
    return _to_out(db, s)


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
    return _to_out(db, s)


@router.patch("/subscriber/daily-loss-limit-pct", response_model=SubscriberSettingsOut)
def set_daily_loss_limit_pct(
    payload: DailyLossLimitPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Daily realized-loss kill switch as a percent of beginning-day
    balance. 0 < pct <= 100. Pass null to disable."""
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
    return _to_out(db, s)


@router.patch("/subscriber/daily-profit-limit-pct", response_model=SubscriberSettingsOut)
def set_daily_profit_limit_pct(
    payload: DailyProfitLimitPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Symmetric to set_daily_loss_limit_pct — profit cap as % of
    beginning-day balance."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.daily_profit_limit_pct
    s.daily_profit_limit_pct = payload.daily_profit_limit_pct
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.daily_profit_limit_pct_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old": str(old) if old is not None else None,
            "new": str(payload.daily_profit_limit_pct) if payload.daily_profit_limit_pct is not None else None,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
    return _to_out(db, s)


@router.patch("/subscriber/max-per-contract", response_model=SubscriberSettingsOut)
def set_max_per_contract(
    payload: MaxPerContractIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """UI-only ceiling — persisted so it survives refresh. NOT enforced
    server-side; copy_engine doesn't read this column."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.max_per_contract
    s.max_per_contract = payload.max_per_contract
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.max_per_contract_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old": str(old) if old is not None else None,
            "new": str(payload.max_per_contract) if payload.max_per_contract is not None else None,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    return _to_out(db, s)


@router.patch("/subscriber/max-account-pct", response_model=SubscriberSettingsOut)
def set_max_account_pct(
    payload: MaxAccountPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """% of current Alpaca equity that, if today's P&L breaches as a loss,
    auto-pauses copy. Enforced by ``services.pnl_poller`` every 60s using
    fresh equity from Alpaca — the dollar threshold floats with account
    size instead of being a fixed cap."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.max_account_pct_per_day
    s.max_account_pct_per_day = payload.max_account_pct_per_day
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.max_account_pct_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old": str(old) if old is not None else None,
            "new": str(payload.max_account_pct_per_day) if payload.max_account_pct_per_day is not None else None,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
    return _to_out(db, s)


@router.patch("/subscriber/auto-liquidation-limit", response_model=SubscriberSettingsOut)
def set_auto_liquidation_limit(
    payload: AutoLiquidationLimitIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Subscriber-set hard floor on account equity. Pass null to disable.

    When pnl_poller observes broker equity <= this value, every open
    position on the subscriber's broker is closed at market AND
    copy_enabled flips to False. Unlike the daily limits, this does NOT
    auto-resume next day — the subscriber must manually re-enable copy.

    Clearing the limit (null) does NOT clear ``auto_liquidated_at`` —
    that stamp persists as an audit record of the last trigger.
    """
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.auto_liquidation_limit
    s.auto_liquidation_limit = payload.auto_liquidation_limit
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.auto_liquidation_limit_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old": str(old) if old is not None else None,
            "new": str(payload.auto_liquidation_limit) if payload.auto_liquidation_limit is not None else None,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
    return _to_out(db, s)


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
    return _to_out(db, s)


def _normalize_symbols(syms: list[str]) -> list[str]:
    """Uppercase, strip, drop empties + duplicates, preserve first-seen
    order. Caps at 200 (defense in depth — Pydantic already enforces
    this, but a malformed direct DB write shouldn't blow up callers)."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in syms:
        s = (raw or "").strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= 200:
            break
    return out


@router.patch("/subscriber/symbol-filter", response_model=SubscriberSettingsOut)
def set_symbol_filter(
    payload: SymbolFilterIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Replace one or both per-subscriber symbol filter lists. Either
    field may be omitted — only the supplied list replaces the stored
    one. Each list is uppercased + deduped server-side. Empty list means
    "no filter applied" for that direction (the historic behaviour)."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")

    changes: dict[str, list[str]] = {}
    if payload.symbol_exclusion_list is not None:
        new_excl = _normalize_symbols(payload.symbol_exclusion_list)
        if list(s.symbol_exclusion_list or []) != new_excl:
            changes["symbol_exclusion_list"] = new_excl
            s.symbol_exclusion_list = new_excl
    if payload.symbol_inclusion_list is not None:
        new_incl = _normalize_symbols(payload.symbol_inclusion_list)
        if list(s.symbol_inclusion_list or []) != new_incl:
            changes["symbol_inclusion_list"] = new_incl
            s.symbol_inclusion_list = new_incl

    if changes:
        audit.record(
            db,
            actor_user_id=user.id,
            action="subscriber.symbol_filter_changed",
            entity_type="subscriber_settings",
            entity_id=user.id,
            metadata=changes,
            ip_address=client_ip(request),
        )
    db.commit()
    db.refresh(s)
    # Bust the per-trader subscriber cache so copy_engine reads the new
    # filter on the very next fanout (otherwise it could keep using a
    # stale snapshot for a few seconds).
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
    return _to_out(db, s)


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
    return [
        {
            "id": str(t.id),
            "display_name": t.display_name,
            "email": t.email,
            "business_name": t.business_name,
        }
        for t in rows
    ]
