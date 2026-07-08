from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user, require_subscriber, require_trader
from app.database import get_db
from app.models.follow_request import FollowRequest, FollowRequestStatus
from app.models.notification_preference import NOTIFY_EVENTS, NotificationPreference
from app.models.settings import RetryInterval, SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.schemas.settings import (
    AutoLiquidationLimitIn,
    CopyTraderBracketIn,
    DailyLossLimitIn,
    DailyLossLimitPctIn,
    DailyProfitLimitIn,
    DailyProfitLimitPctIn,
    FollowTraderIn,
    MaxAccountPctIn,
    MaxPerContractIn,
    PositionSlPctIn,
    PositionTpPctIn,
    RetryIntervalIn,
    SubscriberSelfMultiplierIn,
    SubscriberSettingsOut,
    SubscriberToggleIn,
    SymbolFilterIn,
    TraderSettingsOut,
    TraderToggleIn,
)
from app.services.pnl import today_realized_pnl
from app.services.sms import check_phone_verification, start_phone_verification
from app.services import audit, cache, notifications

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
        retry_max_attempts=s.retry_max_attempts,
        symbol_exclusion_list=list(s.symbol_exclusion_list or []),
        symbol_inclusion_list=list(s.symbol_inclusion_list or []),
        max_per_contract=s.max_per_contract,
        max_account_pct_per_day=s.max_account_pct_per_day,
        auto_liquidation_limit=s.auto_liquidation_limit,
        auto_liquidated_at=s.auto_liquidated_at,
        daily_loss_limit_pct=s.daily_loss_limit_pct,
        daily_profit_limit_pct=s.daily_profit_limit_pct,
        position_tp_pct=s.position_tp_pct,
        position_sl_pct=s.position_sl_pct,
        copy_trader_bracket=s.copy_trader_bracket,
    )


@router.get("/subscriber", response_model=SubscriberSettingsOut)
def get_subscriber_settings(
    db: Session = Depends(get_db), user: User = Depends(require_subscriber)
) -> SubscriberSettingsOut:
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    return _to_out(db, s)


@router.post("/subscriber/reset", response_model=SubscriberSettingsOut)
def reset_subscriber_settings(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Reset the subscriber's risk/config knobs to their model defaults.

    Scope is CONFIG ONLY — deliberately does not touch the active copy
    setup: ``following_trader_id`` and ``copy_enabled`` are left as-is, and
    the internal pause stamps (``pnl_auto_paused_at`` / ``auto_liquidated_at``)
    are untouched so an in-force auto-pause isn't silently cleared. Mirrors
    the defaults declared on ``SubscriberSettings``; keep this in sync if a
    new configurable column is added.
    """
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")

    s.multiplier = Decimal("1.000")
    s.daily_loss_limit = None
    s.daily_profit_limit = None
    s.daily_loss_limit_pct = None
    s.daily_profit_limit_pct = None
    s.auto_liquidation_limit = None
    s.max_per_contract = None
    s.max_account_pct_per_day = None
    s.position_tp_pct = None
    s.position_sl_pct = None
    s.copy_trader_bracket = False
    s.retry_interval_open = RetryInterval.NEVER
    s.retry_interval_close = RetryInterval.NEVER
    s.retry_max_attempts = 1
    s.symbol_exclusion_list = []
    s.symbol_inclusion_list = []

    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.settings_reset",
        entity_type="subscriber_settings",
        entity_id=user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    # Bust the fanout cache so copy_engine picks up the wiped multiplier /
    # filters on the next mirror instead of a stale snapshot.
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
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


@router.patch("/subscriber/position-tp-pct", response_model=SubscriberSettingsOut)
def set_position_tp_pct(
    payload: PositionTpPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Per-position take-profit % applied to every open position.
    Pass null to disable. Enforced by pnl_poller — see
    app.services.position_enforcer for the close mechanics.

    Per-position only: a triggered close does NOT pause copy_enabled
    (other positions and new mirrors keep flowing). For account-wide
    pauses, use the daily kill switches; for full-account liquidation,
    use auto_liquidation_limit.
    """
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.position_tp_pct
    s.position_tp_pct = payload.position_tp_pct
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.position_tp_pct_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old": str(old) if old is not None else None,
            "new": str(payload.position_tp_pct) if payload.position_tp_pct is not None else None,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
    return _to_out(db, s)


@router.patch("/subscriber/position-sl-pct", response_model=SubscriberSettingsOut)
def set_position_sl_pct(
    payload: PositionSlPctIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Per-position stop-loss % applied to every open position. Pass
    null to disable. Symmetric to set_position_tp_pct — same audit +
    cache-bust + response shape."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.position_sl_pct
    s.position_sl_pct = payload.position_sl_pct
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.position_sl_pct_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={
            "old": str(old) if old is not None else None,
            "new": str(payload.position_sl_pct) if payload.position_sl_pct is not None else None,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(s)
    if s.following_trader_id:
        cache.invalidate_subscribers_for_trader(s.following_trader_id)
    return _to_out(db, s)


@router.patch("/subscriber/copy-trader-bracket", response_model=SubscriberSettingsOut)
def set_copy_trader_bracket(
    payload: CopyTraderBracketIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SubscriberSettingsOut:
    """Toggle whether this subscriber copies the trader's per-trade SL/TP
    (re-anchored onto their own fill) instead of their own per-position
    TP/SL %. Cache-busts so the fanout path sees the new flag immediately."""
    s = db.get(SubscriberSettings, user.id)
    if not s:
        raise HTTPException(404, "settings_missing")
    old = s.copy_trader_bracket
    s.copy_trader_bracket = payload.copy_trader_bracket
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.copy_trader_bracket_changed",
        entity_type="subscriber_settings",
        entity_id=user.id,
        metadata={"old": bool(old), "new": bool(payload.copy_trader_bracket)},
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

    changes: dict[str, object] = {}
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
    if payload.retry_max_attempts is not None:
        if payload.retry_max_attempts != s.retry_max_attempts:
            changes["retry_max_attempts"] = payload.retry_max_attempts
            s.retry_max_attempts = payload.retry_max_attempts

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
    # Manual re-enable (going True) acknowledges and clears any in-flight
    # auto-pause markers. Two reasons:
    #   1. Avoids a spurious "Copy trading auto-resumed for the new day"
    #      toast at the next UTC midnight, when the auto-resume sweep
    #      would otherwise see a stale `pnl_auto_paused_at` and emit
    #      `copy.auto_resumed` even though the user already re-enabled.
    #   2. Prevents the auto-resume sweep from overriding a SUBSEQUENT
    #      auto_liquidation_limit hit. Without this, the timeline
    #      "hit daily-loss → manually re-enable → hit auto-liquidation"
    #      would leave both `pnl_auto_paused_at` (yesterday) and
    #      `auto_liquidated_at` (today) set, and the sweep would
    #      incorrectly resume copy off the stale daily-limit stamp,
    #      undoing the sticky liquidation state.
    # Going False (manual pause) leaves the markers alone — they're
    # already null in that case, or already accurate.
    if payload.copy_enabled:
        s.pnl_auto_paused_at = None
        s.auto_liquidated_at = None
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
        # Approval gate: a subscriber may only follow a trader who has approved
        # their follow request — UNLESS the trader has "auto-allow" on, in which
        # case anyone may follow directly with no request. Existing follows were
        # grandfathered in as approved by the follow_requests migration.
        ts = db.get(TraderSettings, payload.trader_id)
        if not (ts and ts.auto_approve_follows):
            approved = db.execute(
                select(FollowRequest).where(
                    FollowRequest.subscriber_id == user.id,
                    FollowRequest.trader_id == payload.trader_id,
                    FollowRequest.status == FollowRequestStatus.APPROVED,
                )
            ).scalar_one_or_none()
            if approved is None:
                raise HTTPException(403, "follow_not_approved")
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
    changes: dict[str, object] = {}
    if payload.trading_enabled is not None and payload.trading_enabled != s.trading_enabled:
        s.trading_enabled = payload.trading_enabled
        changes["trading_enabled"] = payload.trading_enabled
    if payload.auto_approve_follows is not None and payload.auto_approve_follows != s.auto_approve_follows:
        s.auto_approve_follows = payload.auto_approve_follows
        changes["auto_approve_follows"] = payload.auto_approve_follows
    if changes:
        audit.record(
            db,
            actor_user_id=user.id,
            action="trader.settings_changed",
            entity_type="trader_settings",
            entity_id=user.id,
            metadata=changes,
            ip_address=client_ip(request),
        )
    db.commit()
    db.refresh(s)
    return s


@router.get("/traders", response_model=list[dict])
def list_available_traders(db: Session = Depends(get_db), _: User = Depends(current_user)) -> list[dict]:
    """Subscribers use this to find the trader to follow. Includes each
    trader's ``auto_approve_follows`` so the UI can show a direct Follow button
    (auto-allow) vs a Request-to-follow button (approval required)."""
    rows = db.execute(
        select(User, TraderSettings.auto_approve_follows)
        .join(TraderSettings, TraderSettings.user_id == User.id, isouter=True)
        .where(User.role == UserRole.TRADER, User.is_active.is_(True))
    ).all()
    return [
        {
            "id": str(t.id),
            "display_name": t.display_name,
            "email": t.email,
            "business_name": t.business_name,
            "auto_approve_follows": bool(auto),
        }
        for t, auto in rows
    ]


# ── Notification preferences + phone verification ─────────────────────────────
# Available to every role (traders and subscribers both get notifications), so
# these use `current_user` rather than a role-specific dependency.

class NotificationPrefsOut(BaseModel):
    email_enabled: bool
    sms_enabled: bool
    event_overrides: dict
    phone_number: str | None
    phone_verified: bool
    sms_available: bool  # Twilio configured server-side (drives UI enablement)


class NotificationPrefsIn(BaseModel):
    email_enabled: bool | None = None
    sms_enabled: bool | None = None
    # {"order.filled": {"email": true, "sms": false}, ...}
    event_overrides: dict | None = None


class PhoneIn(BaseModel):
    phone_number: str = Field(min_length=8, max_length=20)


class PhoneVerifyIn(BaseModel):
    code: str = Field(min_length=4, max_length=10)


def _prefs_out(user: User, pref: NotificationPreference) -> NotificationPrefsOut:
    from app.services.sms import verify_configured  # noqa: PLC0415
    return NotificationPrefsOut(
        email_enabled=pref.email_enabled,
        sms_enabled=pref.sms_enabled,
        event_overrides=pref.event_overrides or {},
        phone_number=user.phone_number,
        phone_verified=user.phone_verified,
        sms_available=verify_configured(),
    )


@router.get("/notifications", response_model=NotificationPrefsOut)
def get_notification_prefs(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> NotificationPrefsOut:
    pref = notifications.get_or_create_prefs(db, user.id)
    db.commit()
    return _prefs_out(user, pref)


@router.patch("/notifications", response_model=NotificationPrefsOut)
def update_notification_prefs(
    payload: NotificationPrefsIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> NotificationPrefsOut:
    pref = notifications.get_or_create_prefs(db, user.id)
    if payload.email_enabled is not None:
        pref.email_enabled = payload.email_enabled
    if payload.sms_enabled is not None:
        # SMS master can't be turned on without a verified number.
        if payload.sms_enabled and not user.phone_verified:
            raise HTTPException(400, "phone_not_verified")
        pref.sms_enabled = payload.sms_enabled
    if payload.event_overrides is not None:
        # Whitelist to known events + channels so the UI can't stash junk.
        cleaned: dict = {}
        for ev, chans in payload.event_overrides.items():
            if ev not in NOTIFY_EVENTS or not isinstance(chans, dict):
                continue
            cleaned[ev] = {c: bool(v) for c, v in chans.items() if c in ("email", "sms")}
        pref.event_overrides = cleaned
    audit.record(
        db, actor_user_id=user.id, action="notifications.prefs_updated",
        entity_type="user", entity_id=user.id,
    )
    db.commit()
    return _prefs_out(user, pref)


@router.post("/phone")
def set_phone(
    payload: PhoneIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Save a phone number and send a Twilio Verify OTP to it. The number is
    stored unverified; SMS notifications stay off until /phone/verify succeeds."""
    phone = payload.phone_number.strip().replace(" ", "")
    # E.164: leading '+' then 8–15 digits. Keeps Twilio from 400-ing on obvious junk.
    if not (phone.startswith("+") and phone[1:].isdigit() and 8 <= len(phone[1:]) <= 15):
        raise HTTPException(422, "phone_must_be_e164")  # e.g. +15551234567
    user.phone_number = phone
    user.phone_verified = False
    user.phone_verified_at = None
    db.commit()

    ok, detail = start_phone_verification(phone)
    audit.record(
        db, actor_user_id=user.id, action="notifications.phone_set",
        entity_type="user", entity_id=user.id, metadata={"otp_sent": ok},
    )
    db.commit()
    if not ok:
        raise HTTPException(503, f"otp_send_failed:{detail}")
    return {"ok": True, "detail": "otp_sent"}


@router.post("/phone/verify", response_model=NotificationPrefsOut)
def verify_phone(
    payload: PhoneVerifyIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> NotificationPrefsOut:
    """Check the OTP; on success mark the phone verified and default SMS on."""
    if not user.phone_number:
        raise HTTPException(400, "no_phone_on_file")
    if not check_phone_verification(user.phone_number, payload.code.strip()):
        raise HTTPException(400, "invalid_code")
    user.phone_verified = True
    user.phone_verified_at = datetime.now(timezone.utc)
    pref = notifications.get_or_create_prefs(db, user.id)
    pref.sms_enabled = True  # they just added + verified a number — opt them in
    audit.record(
        db, actor_user_id=user.id, action="notifications.phone_verified",
        entity_type="user", entity_id=user.id,
    )
    db.commit()
    return _prefs_out(user, pref)
