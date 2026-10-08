"""Inbound Discord alert sources — STEP 2: connection + real-time listener.

A trader connects a Discord channel that Kopyya monitors for trade alerts. This
router manages the connection lifecycle and serves as the boundary the
``discord-listener`` container talks to. It does NOT parse trades, validate
signals, or place orders — those are steps 4-6, and keeping them out of here is
deliberate: a message is only data until the parser has looked at it.

── Ingestion model ──────────────────────────────────────────────────────────────
We monitor Discord Web as the trader's OWN logged-in account, reading only the
channels that account can already legitimately open. The trader signs in
themselves in a real browser (password + MFA never touch Kopyya) and we store the
resulting session, Fernet-encrypted. Nothing here bypasses Discord
authentication, permissions, MFA or rate limiting. This replaces the step-1
bot-token + Channel-Following model, which could not reach the third-party alert
servers traders actually follow.

Separate from the OUTBOUND webhook broadcast (api/settings.py PATCH
/settings/trader + services/discord_alerts.py), which posts the trader's own
fills TO Discord. Different direction, different storage.

── Two authentication domains ───────────────────────────────────────────────────
Trader routes use the normal JWT + ``require_trader``. The ``/internal/*`` routes
are called by the listener container, which has no user, and authenticate with a
shared token. They are mounted on the same router for locality but must never be
confused: ``/internal/assignments`` hands out decrypted Discord sessions.
"""
from __future__ import annotations

import inspect
import logging
import re
import secrets
import time
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import current_user, require_trader
from app.config import get_settings
from app.database import get_db
from app.models.discord_account import DiscordAccount
from app.models.discord_alert_source import DiscordAlertSource
from app.models.order import Order, OrderSide, OrderStatus, OrderType
from app.models.discord_message import DiscordMessage, DiscordMessageStatus, SignalDecision
from app.models.user import User, UserRole
from app.schemas.pagination import Page
from app.schemas.discord import (
    DiscordAssignmentOut,
    DiscordLoginCompleteIn,
    DiscordLoginOut,
    DiscordLoginQrIn,
    DiscordLoginRequestOut,
    DiscordLoginStatusIn,
    DiscordPairClaimIn,
    DiscordPairClaimOut,
    DiscordPairCompleteIn,
    DiscordDecisionOut,
    DiscordSettingsIn,
    DiscordSettingsOut,
    DiscordTrimRow,
    DiscordPairOut,
    DiscordSignalOut,
    DiscordIngestOut,
    DiscordListenerStatusIn,
    DiscordMessageBatchIn,
    DiscordMessageOut,
    DiscordSessionIn,
    DiscordSessionInfo,
    DiscordSourceIn,
    DiscordSourceOut,
    DiscordSourceUpdateIn,
    ChannelSettingsIn,
    ChannelSettingsOut,
    DiscordSelfAlertIn,
    DiscordSelfAlertOut,
    PauseAllIn,
    PauseAllOut,
)
from app.services import (
    discord_execution,
    discord_position_guard as guards,
    price_override,
    discord_edit,
    discord_channel_settings,
    discord_ingest,
    discord_ladder,
    discord_login,
    discord_pairing,
    discord_schedule,
    discord_subscribers,
    events,
)
from app.services.discord_session import (
    DiscordSessionError,
    decrypt_session,
    describe_session,
    encrypt_session,
    parse_channel_url,
    validate_storage_state,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/discord-sources", tags=["discord-sources"])

# The virtual "Self" channel — see submit_self_alert. Defined up here because
# list_sources filters on it long before that endpoint appears.
_SELF_CHANNEL_ID = "self"
_SELF_LABEL = "Self"


def require_discord_member(
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> User:
    """A Discord trader, or a subscriber of one.

    Routes a subscriber shares with the trader — their channel switches, their
    own alert-handling settings, signals and approvals — take this instead of
    require_trader + _require_feature. Adding, connecting and removing channels
    stay trader-only.
    """
    if not get_settings().discord_listener_enabled:
        raise HTTPException(503, "discord_listener_disabled")
    if user.role == UserRole.TRADER:
        if not user.discord_enabled:
            raise HTTPException(403, "discord_not_enabled")
        return user
    if discord_subscribers.followed_discord_trader(db, user) is None:
        raise HTTPException(403, "discord_not_enabled")
    return user


def _require_feature(user: User = Depends(require_trader)) -> None:
    """Gate every trader Discord route two ways:

    - 503 when the inbound Discord feature is switched off environment-wide
      (no listener container), so a trader can't connect a source that would
      sit at 'connecting' forever with no explanation.
    - 403 when THIS trader isn't allow-listed for Discord. It's an opt-in,
      admin-enabled feature (PATCH /api/admin/users/{id}/discord-enabled); off
      by default, so non-enabled traders are API-blocked here even if they hit
      the endpoints directly.
    """
    if not get_settings().discord_listener_enabled:
        raise HTTPException(503, "discord_listener_disabled")
    if not user.discord_enabled:
        raise HTTPException(403, "discord_not_enabled")


def require_listener_token(
    x_kopyaa_listener_token: str = Header(default=""),
) -> None:
    """Authenticate the discord-listener container on ``/internal/*``.

    The listener runs with no database and no broker access, so this token is
    its entire authority — it can read Discord sessions and post messages,
    nothing more. A BLANK configured token disables these routes outright rather
    than accepting everything: an unset secret must never degrade into "no auth
    required". Compared with ``compare_digest`` so a wrong token can't be
    recovered by timing the response.
    """
    expected = get_settings().discord_listener_token
    if not expected:
        raise HTTPException(503, "discord_listener_not_configured")
    if not secrets.compare_digest(x_kopyaa_listener_token or "", expected):
        raise HTTPException(401, "invalid_listener_token")


def _cancel_stop_order(db: Session, user: User):
    """Cancel one of this trader's resting stop orders, by our order id.

    Used to free the contracts a stop reserves before an exit is placed. A
    broker cancel that fails because the order is already gone is the outcome we
    wanted anyway, so it is logged rather than raised.
    """
    def _cancel(order_id) -> None:
        from app.brokers import adapter_for  # noqa: PLC0415
        from app.models.broker_account import BrokerAccount  # noqa: PLC0415
        from app.models.order import Order, OrderStatus  # noqa: PLC0415
        from app.services.crypto import decrypt_json  # noqa: PLC0415

        order = db.get(Order, order_id)
        if order is None:
            return
        if order.broker_order_id:
            acct = db.get(BrokerAccount, order.broker_account_id)
            if acct is not None:
                try:
                    adapter_for(acct, decrypt_json(acct.encrypted_credentials)).cancel_order(
                        order.broker_order_id
                    )
                except Exception:  # noqa: BLE001
                    log.warning(
                        "discord: stop cancel failed for order %s", order_id, exc_info=True
                    )
        order.status = OrderStatus.CANCELED

    return _cancel


def _setting(ts, name: str, default: str) -> Decimal:
    """A Decimal setting, falling back when the row or column is unset."""
    raw = getattr(ts, name, None) if ts is not None else None
    return Decimal(str(raw)) if raw is not None else Decimal(default)


def _reopen(guard) -> None:
    """Undo a full exit's retirement when its order was never placed."""
    if guard is None:
        return
    guard.closed_at = None
    guard.closed_reason = None
    guards.rollback_exit(guard)


def _trim_config(ts) -> "guards.TrimConfig":
    """The trader's exit ladder as configured — what a live trim and the
    Simulated Prices dry run both measure against. Any number of trims; read
    through services/discord_ladder so the storage split never leaks."""
    from app.services import discord_ladder  # noqa: PLC0415

    return discord_ladder.trim_config(ts)


def _plain(value) -> str | None:
    """Decimal -> the shortest exact string a human would write.

    Numeric(9,4) round-trips as "20.0000", which reads like precision that isn't
    meaningful for a percentage or a dollar cap. normalize() alone would give
    "2E+1", so format with 'f' to keep it positional.
    """
    if value is None:
        return None
    from decimal import Decimal as _D  # noqa: PLC0415

    return format(_D(str(value)).normalize(), "f")


def _auto_approve(db: Session, user_id: uuid.UUID, source_id: uuid.UUID | None = None) -> bool:
    """Is Discord execution set to auto — for this channel, when given (its own
    setting, or the account's while it follows them), else for the account?

    Defaults to manual when no settings row exists — an alert must never be
    cleared for execution because a row was missing.
    """
    ts = discord_channel_settings.effective(db, user_id, source_id)
    return bool(ts and (ts.discord_execution_mode or "manual").lower() == "auto")


def _primary_account(db: Session, user: User, *, create: bool = False) -> DiscordAccount | None:
    """This trader's Discord account, created on demand.

    One account covers every channel it can read — which is the whole point of
    moving the session off the source. Traders with two Discord accounts get a
    second row, but the common case never has to choose.
    """
    acct = db.execute(
        select(DiscordAccount)
        .where(DiscordAccount.user_id == user.id)
        .order_by(DiscordAccount.created_at.asc())
    ).scalars().first()
    if acct is None and create:
        acct = DiscordAccount(user_id=user.id, label="Discord account", status="needs_login")
        db.add(acct)
        db.flush()
    return acct


def _store_session(db: Session, src: DiscordAlertSource, state: dict) -> DiscordAccount | None:
    """Attach a captured session to the channel's ACCOUNT and bring every
    channel that account reads online.

    All three capture routes (Connector pairing, QR, manual upload) funnel
    through here so none of them can drift back to per-channel sessions.
    """
    acct = src.account
    if acct is None:
        return None
    acct.encrypted_session = encrypt_session(state)
    acct.session_captured_at = datetime.now(timezone.utc)
    acct.status = "connected"
    acct.last_error = None
    for sibling in acct.sources:
        if sibling.is_enabled and sibling.status in ("needs_login", "error"):
            # 'connecting', not 'connected': only the listener actually opening
            # the channel proves Discord still accepts the session.
            sibling.status = "connecting"
            sibling.last_error = None
    return acct


def _get_owned(db: Session, user: User, source_id: uuid.UUID) -> DiscordAlertSource:
    src = db.get(DiscordAlertSource, source_id)
    if src is None or src.user_id != user.id:
        raise HTTPException(404, "not_found")
    return src


def _to_out(src: DiscordAlertSource) -> DiscordSourceOut:
    """ORM → response. The session is summarised, never included.

    ``session`` is derived rather than stored, so it carries a default on the
    schema and is overwritten here — every response goes through this function,
    so the default is never what the caller actually sees.
    """
    out = DiscordSourceOut.model_validate(src)
    out.schedule_summary = discord_schedule.describe(
        mode=src.schedule_mode,
        start=src.schedule_start,
        end=src.schedule_end,
        timezone=src.schedule_timezone,
    )
    acct = src.account
    out.session = DiscordSessionInfo(
        **describe_session(
            acct.encrypted_session if acct else None,
            acct.session_captured_at if acct else None,
        )
    )
    return out


# ── Trader routes ────────────────────────────────────────────────────────────

@router.get("", response_model=list[DiscordSourceOut])
def list_sources(
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> list[DiscordSourceOut]:
    if user.role != UserRole.TRADER:
        pairs = discord_subscribers.sync_mirrors(db, user)
        db.commit()
        return [_with_pills(db, user, m.id, _mirror_out(m, p)) for m, p in pairs]
    rows = db.execute(
        select(DiscordAlertSource)
        .where(
            DiscordAlertSource.user_id == user.id,
            # The Self channel is a plumbing detail, not a channel the trader
            # connected. Listing it would offer Change channel / Disconnect /
            # a watch schedule for something with no Discord behind it — and
            # disconnecting it would break the composer with no way back.
            DiscordAlertSource.channel_id != _SELF_CHANNEL_ID,
        )
        .order_by(DiscordAlertSource.created_at.desc())
    ).scalars()
    return [_with_pills(db, user, r.id, _to_out(r)) for r in rows]


def _pausable(db: Session, user: User) -> list[DiscordAlertSource]:
    """Every channel "Pause ALL channels" acts on: the trader's own, or a
    subscriber's copies of the trader's. Never the Self channel — that is where
    the trader replays an alert by hand."""
    if user.role != UserRole.TRADER:
        discord_subscribers.sync_mirrors(db, user)
    return list(db.execute(
        select(DiscordAlertSource).where(
            DiscordAlertSource.user_id == user.id,
            DiscordAlertSource.channel_id != _SELF_CHANNEL_ID,
        )
    ).scalars())


def _connect_status(src: DiscordAlertSource) -> str:
    """What an enabled channel shows until the listener reports in."""
    return "connecting" if (src.account and src.account.encrypted_session) else "needs_login"


@router.get("/pause-all", response_model=PauseAllOut)
def pause_all_state(
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> PauseAllOut:
    """Is "Pause ALL channels" in effect?"""
    rows = _pausable(db, user)
    db.commit()
    return PauseAllOut(paused=any(r.paused_by_pause_all for r in rows))


@router.post("/pause-all", response_model=PauseAllOut)
def pause_all(
    payload: PauseAllIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> PauseAllOut:
    """Pause: turn off every channel that is on, remembering which. Resume: turn
    those back on — a channel that was already off before the pause stays off.
    Open positions and their exits are not touched."""
    from app.services import audit  # noqa: PLC0415
    from app.api.deps import client_ip  # noqa: PLC0415

    changed = 0
    for src in _pausable(db, user):
        if payload.paused and src.is_enabled:
            src.is_enabled = False
            src.paused_by_pause_all = True
            if src.parent_source_id is None:
                src.status = "disconnected"
            changed += 1
        elif not payload.paused and src.paused_by_pause_all:
            src.is_enabled = True
            src.paused_by_pause_all = False
            if src.parent_source_id is None:
                src.status = _connect_status(src)
            changed += 1
    audit.record(
        db, actor_user_id=user.id,
        action="discord.channels_paused" if payload.paused else "discord.channels_resumed",
        entity_type="user", entity_id=user.id, metadata={"channels": changed},
        ip_address=client_ip(request),
    )
    db.commit()
    log.info("discord: %s %d channel(s) for user %s",
             "paused" if payload.paused else "resumed", changed, user.id)
    return PauseAllOut(paused=payload.paused and changed > 0, changed=changed)


def _with_pills(db: Session, user: User, src_id: uuid.UUID, out: DiscordSourceOut) -> DiscordSourceOut:
    """Fill the Entry / Exit pills from the settings that apply to this channel
    (its own, or the account's while it follows them)."""
    from app.models.settings import TraderSettings  # noqa: PLC0415

    eff = discord_channel_settings.effective(db, user.id, src_id)
    account = db.get(TraderSettings, user.id)
    n = int(getattr(eff, "discord_quantity_multiplier", None) or 1)
    kind = "Market" if discord_channel_settings.entry_order_type(db, src_id) == "market" else "Limit"
    dollars = getattr(eff, "discord_size_dollars", None)
    if getattr(eff, "discord_size_mode", None) == "dollars" and dollars:
        entry = f"{kind} · ${Decimal(str(dollars)).normalize():f}"
    else:
        entry = f"{kind} · {n} contract{'s' if n != 1 else ''}"
    if not getattr(eff, "discord_live_trading", False):
        entry += " · Test"
    if (getattr(eff, "discord_execution_mode", None) or "manual").lower() != "auto":
        entry += " · Review"

    if discord_channel_settings.exits_manual(eff):
        exit_ = "Manual"
    elif getattr(account, "discord_exit_engine", None) == "ai":
        exit_ = "AI trimming"
    elif getattr(eff, "discord_auto_trim", False) or getattr(eff, "discord_tp_orders", False):
        from app.services import discord_ladder  # noqa: PLC0415

        gates = [r.profit_gate_pct for r in discord_ladder.rungs(eff) if r.profit_gate_pct > 0]
        kind = "Take-profit orders" if getattr(eff, "discord_tp_orders", False) else "Auto-trim"
        exit_ = (f"{kind} " + " / ".join(f"{_plain(g)}%" for g in gates)) if gates \
            else f"{kind} (no profit targets set)"
    else:
        exit_ = "On trim alerts"
    out.entry_summary, out.exit_summary = entry, exit_
    return out


def _mirror_out(mirror: DiscordAlertSource, parent: DiscordAlertSource) -> DiscordSourceOut:
    """A subscriber's view of a trader channel: the channel's live state and
    schedule as the trader runs it, with the subscriber's own on/off switch.
    The trader's Discord session is theirs — only whether one exists is said."""
    out = _to_out(parent)
    out.id = mirror.id
    out.is_enabled = mirror.is_enabled
    out.mirrored = True
    out.session = DiscordSessionInfo(present=out.session.present, cookie_count=0)
    return out


@router.post("", response_model=DiscordSourceOut, status_code=status.HTTP_201_CREATED)
def create_source(
    payload: DiscordSourceIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
) -> DiscordSourceOut:
    """Register a channel to monitor. Starts at 'needs_login' — the trader
    completes the one-time Discord sign-in separately, and only then does the
    listener have a session to open the channel with."""
    try:
        guild_id, channel_id = parse_channel_url(payload.channel_url)
    except DiscordSessionError as exc:
        raise HTTPException(400, f"invalid_channel_url: {exc}")

    # Attach to the trader's Discord account. If it's already connected the new
    # channel is live immediately — no second sign-in.
    acct = _primary_account(db, user, create=True)
    connected = bool(acct and acct.encrypted_session)
    src = DiscordAlertSource(
        user_id=user.id,
        account_id=acct.id if acct else None,
        label=payload.label.strip(),
        guild_id=guild_id,
        channel_id=channel_id,
        is_enabled=True,
        status="connecting" if connected else "needs_login",
    )
    db.add(src)
    try:
        db.commit()
    except IntegrityError:
        # uq_discord_source_user_channel — this trader already watches it.
        # Two sources on one channel would ingest every alert twice, under
        # different source ids that the idempotency key can't see across.
        db.rollback()
        raise HTTPException(409, "channel_already_connected")
    db.refresh(src)
    return _to_out(src)


# ── simulated prices (testing only) ─────────────────────────────────────────
# Lets a trader pin a contract's price so the trim ladder's stops and trailing
# exits can be exercised against a quiet market. Gated on its own flag, OFF by
# default, because a pinned price feeds the REAL enforcement path: a pin below a
# stop places a REAL order, filled at the REAL price, not the pinned one.

class PinnedPositionOut(BaseModel):
    """One open position, with whatever the ladder currently knows about it."""

    key: str
    symbol: str
    option_strike: str | None = None
    option_right: str | None = None
    option_expiry: str | None = None
    quantity: str
    broker_price: str | None = None
    avg_entry_price: str | None = None
    pinned_price: str | None = None
    entry_price: str | None = None
    stop_price: str | None = None
    trail_qty: str | None = None
    trail_amount: str | None = None
    peak_price: str | None = None
    rung: int = 0
    ladder_history: list["LadderHistoryOut"] = Field(default_factory=list)


class LadderHistoryOut(BaseModel):
    """A ladder exit from this position's current entry onward."""

    # Stable across refreshes, so the page can hide what it has already shown.
    id: str
    rung: int
    quantity: str
    filled_quantity: str
    quantity_before: str
    quantity_remaining: str
    stop_quantity: str
    fill_price: str | None = None
    status: str
    note: str | None = None
    # None for a rung that only moved the stop: nothing records when it ran.
    happened_at: datetime | None = None


class PinIn(BaseModel):
    key: str
    # Empty or null clears the pin and hands the position back to the broker.
    price: str | None = None


def _require_pin_feature(user: User = Depends(require_trader)) -> None:
    if not price_override.enabled():
        raise HTTPException(503, "price_override_disabled")


# How long a retired guard still speaks for a position the broker reports: its
# final exit is submitted but not yet filled. Past this, it is a previous run.
_RETIRED_GUARD_GRACE = timedelta(minutes=15)


def _ladder_history(guard, sells, position_qty) -> list[LadderHistoryOut]:
    """This guard's run of the ladder, oldest first, numbered in the order it ran.

    Three kinds of rung leave three kinds of trace. A rung that sold has an
    Order. A rung that parked the rest on a trailing exit has only the guard's
    ``armed_at``. A rung that sold nothing (an alert under its gate) only moved
    the stop and leaves no timestamp anywhere, so it is listed untimed after
    the rest — which keeps the count equal to the guard's ``sell_count``.
    """
    sells = [e for e in sells if e.created_at >= guard.created_at]
    items: list[tuple[datetime, Order | None]] = [(e.created_at, e) for e in sells]
    if (
        guard.trail_qty is not None
        and guard.armed_at is not None
        and guard.armed_at >= guard.created_at
    ):
        items.append((guard.armed_at, None))
    items.sort(key=lambda item: item[0])

    # Rebuild the size before each trim from the broker's current remainder
    # plus every fill in this run, rather than storing a second counter.
    remaining = Decimal(str(position_qty or 0)) + sum(
        (Decimal(str(e.filled_quantity or 0)) for e in sells), Decimal(0),
    )
    protected = guard.stop_price is not None or guard.trail_qty is not None
    history: list[LadderHistoryOut] = []
    for at, event in items:
        rung = len(history) + 1
        if event is None:
            history.append(LadderHistoryOut(
                id=f"trail:{guard.id}:{at.isoformat()}",
                rung=rung,
                quantity=_plain(remaining) or "0",
                filled_quantity="0",
                quantity_before=_plain(remaining) or "0",
                quantity_remaining=_plain(remaining) or "0",
                stop_quantity=_plain(guard.trail_qty) or "0",
                status="armed",
                note="trailing stop armed",
                happened_at=at,
            ))
            continue
        before = remaining
        remaining = max(Decimal(0), remaining - Decimal(str(event.filled_quantity or 0)))
        history.append(LadderHistoryOut(
            id=str(event.id),
            rung=rung,
            quantity=_plain(event.quantity) or "0",
            filled_quantity=_plain(event.filled_quantity) or "0",
            quantity_before=_plain(before) or "0",
            quantity_remaining=_plain(remaining) or "0",
            stop_quantity=_plain(remaining if protected else Decimal(0)) or "0",
            fill_price=_plain(event.filled_avg_price) or _plain(event.limit_price),
            status=event.status.value,
            happened_at=event.broker_filled_at or event.closed_at or event.created_at,
        ))
    for rung in range(len(history) + 1, (guard.sell_count or 0) + 1):
        history.append(LadderHistoryOut(
            id=f"stop:{guard.id}:{rung}",
            rung=rung,
            quantity=_plain(remaining) or "0",
            filled_quantity="0",
            quantity_before=_plain(remaining) or "0",
            quantity_remaining=_plain(remaining) or "0",
            stop_quantity=_plain(remaining if protected else Decimal(0)) or "0",
            status="stop_only",
            note="nothing sold — stop only",
        ))
    return history


def _positions_for_screen(db: Session, user: User) -> list[tuple[str, object]]:
    """The trader's open positions as (contract key, position); [] with no broker."""
    from app.brokers import adapter_for  # noqa: PLC0415
    from app.models.broker_account import BrokerAccount  # noqa: PLC0415
    from app.services.crypto import decrypt_json  # noqa: PLC0415

    acct = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().first()
    if acct is None:
        return []

    adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
    try:
        # DISPLAY only -- nothing here decides whether to place an order, so it
        # takes the shared cached read. Webull's quota is ~10 requests per 30s
        # across EVERY endpoint that key touches, and it answers simultaneous
        # position reads with 429 outright; this screen refreshes while the
        # poller and the positions page are reading the same account.
        positions = adapter.get_positions(cached_ok=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Couldn't read positions: {exc}") from exc
    return [
        (price_override.contract_key(
            pos.symbol, pos.option_strike,
            getattr(pos, "option_right", None), pos.option_expiry,
        ), pos)
        for pos in positions
    ]


@router.get("/simulated-prices", response_model=list[PinnedPositionOut])
def list_simulated_prices(
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _f: None = Depends(_require_feature),
    _p: None = Depends(_require_pin_feature),
) -> list[PinnedPositionOut]:
    """Open positions, their live price, and any pin standing on them."""
    from app.models.discord_position_guard import DiscordPositionGuard  # noqa: PLC0415

    positions = [pos for _, pos in _positions_for_screen(db, user)]

    # Resolve each position's guard first: the trim query below is bounded by
    # the oldest of them, so it reads one run's worth of sells, not all history.
    matched = []
    for pos in positions:
        key = price_override.contract_key(
            pos.symbol, pos.option_strike,
            getattr(pos, "option_right", None), pos.option_expiry,
        )
        guard = guards.find(
            db, user.id, pos.symbol, pos.option_strike,
            pos.option_right, pos.option_expiry,
        )
        if guard is None:
            # A final exit retires its guard as soon as it is submitted, but a
            # broker may still report the position until that order fills. Keep
            # that guard's history visible for the short window until it does.
            # Anything retired earlier belongs to a previous run of this
            # contract, and must not be shown against a position opened since.
            guard = db.execute(
                select(DiscordPositionGuard).where(
                    DiscordPositionGuard.user_id == user.id,
                    DiscordPositionGuard.symbol == pos.symbol,
                    DiscordPositionGuard.option_strike == pos.option_strike,
                    DiscordPositionGuard.option_right == pos.option_right,
                    DiscordPositionGuard.option_expiry == pos.option_expiry,
                    DiscordPositionGuard.closed_at
                    >= datetime.now(timezone.utc) - _RETIRED_GUARD_GRACE,
                ).order_by(DiscordPositionGuard.created_at.desc()).limit(1)
            ).scalars().first()
        matched.append((pos, key, guard))

    # A scissors message is a synthetic auto-trim. ``is_partial_close`` is set
    # only by the Discord trim path, and also keeps a trim whose source message
    # has since been removed. A subquery rather than a join: a join repeats an
    # order once per message pointing at it, subtracting its fill twice.
    # Rejected orders are left out — placement failure hands the rung back.
    events_by_key: dict[str, list[Order]] = {}
    run_guards = [g for _, _, g in matched if g is not None]
    if run_guards:
        scissors = select(DiscordMessage.order_id).where(
            DiscordMessage.user_id == user.id,
            DiscordMessage.order_id.is_not(None),
            DiscordMessage.content.like("✂️%"),
        )
        event_rows = db.execute(
            select(Order).where(
                Order.user_id == user.id,
                Order.side == OrderSide.SELL,
                Order.status != OrderStatus.REJECTED,
                Order.symbol.in_(sorted({g.symbol for g in run_guards})),
                Order.created_at >= min(g.created_at for g in run_guards),
                or_(Order.is_partial_close.is_(True), Order.id.in_(scissors)),
            ).order_by(Order.created_at.asc())
        ).scalars()
        for event in event_rows:
            event_key = price_override.contract_key(
                event.symbol, event.option_strike, event.option_right, event.option_expiry,
            )
            events_by_key.setdefault(event_key, []).append(event)

    out: list[PinnedPositionOut] = []
    for pos, key, guard in matched:
        right = getattr(pos.option_right, "value", pos.option_right)
        history = (
            _ladder_history(guard, events_by_key.get(key, []), pos.quantity)
            if guard else []
        )
        out.append(PinnedPositionOut(
            key=key,
            symbol=pos.symbol,
            option_strike=_plain(pos.option_strike),
            option_right=right,
            option_expiry=pos.option_expiry.isoformat() if pos.option_expiry else None,
            quantity=_plain(pos.quantity) or "0",
            broker_price=_plain(getattr(pos, "current_price", None)),
            avg_entry_price=_plain(getattr(pos, "avg_entry_price", None)),
            pinned_price=_plain(price_override.get_pin(user.id, key)),
            entry_price=_plain(guard.entry_price) if guard else None,
            stop_price=_plain(guard.stop_price) if guard else None,
            trail_qty=_plain(guard.trail_qty) if guard else None,
            trail_amount=_plain(guard.trail_amount) if guard else None,
            peak_price=_plain(guard.peak_price) if guard else None,
            rung=(guard.sell_count or 0) if guard else 0,
            ladder_history=history,
        ))
    return out


class DryRunIn(BaseModel):
    key: str
    buy_price: Decimal = Field(gt=0)
    # Percent from buy, one per step. Negative is allowed: a dry run should be
    # able to walk a position down into its stop.
    path: list[Decimal] = Field(min_length=1, max_length=200)
    quantity: Decimal | None = Field(default=None, gt=0)


class DryRunEventOut(BaseModel):
    kind: str
    text: str
    rung: int | None = None
    sold: str = "0"


class DryRunStepOut(BaseModel):
    index: int
    pct: str
    price: str
    gain_pct: str | None = None
    held: str
    stop: str | None = None
    events: list[DryRunEventOut]


class DryRunOut(BaseModel):
    quantity: str
    auto_trim_on: bool
    steps: list[DryRunStepOut]


@router.post("/simulated-prices/dry-run", response_model=DryRunOut)
def dry_run_price_path(
    payload: DryRunIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _f: None = Depends(_require_feature),
) -> DryRunOut:
    """Narrate the exit ladder along a price path. Places nothing, writes nothing.

    Starts from a fresh entry at ``buy_price`` with the trader's current ladder
    settings, so it answers "what would my ladder do on this contract" whatever
    state the live guard is in — including no guard at all. Needs no pin
    feature: nothing here reaches enforcement or a broker.
    """
    from app.models.settings import TraderSettings  # noqa: PLC0415

    ts = db.get(TraderSettings, user.id)
    qty = payload.quantity
    if qty is None:
        match = next(
            (r for r in _positions_for_screen(db, user) if r[0] == payload.key), None,
        )
        if match is None:
            raise HTTPException(404, "That position is no longer open.")
        qty = abs(Decimal(str(match[1].quantity or 0)))
    if qty <= 0:
        raise HTTPException(400, "Nothing held to simulate.")

    from app.services import discord_auto_trim, ladder_simulator  # noqa: PLC0415

    steps = ladder_simulator.simulate(
        ts, _trim_config(ts), payload.buy_price, qty, payload.path,
    )
    return DryRunOut(
        quantity=_plain(qty) or "0",
        auto_trim_on=discord_auto_trim._enabled(ts),
        steps=[
            DryRunStepOut(
                index=st.index,
                pct=_plain(st.pct) or "0",
                price=_plain(st.price) or "0",
                gain_pct=_plain(st.gain_pct.quantize(Decimal("0.01"))) if st.gain_pct is not None else None,
                held=_plain(st.held) or "0",
                stop=_plain(st.stop),
                events=[
                    DryRunEventOut(kind=e.kind, text=e.text, rung=e.rung, sold=_plain(e.sold) or "0")
                    for e in st.events
                ],
            )
            for st in steps
        ],
    )


@router.post("/simulated-prices", response_model=list[PinnedPositionOut])
def set_simulated_price(
    payload: PinIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _f: None = Depends(_require_feature),
    _p: None = Depends(_require_pin_feature),
) -> list[PinnedPositionOut]:
    """Pin or clear one contract's price, then hand back the refreshed list."""
    raw = (payload.price or "").strip()
    if not raw:
        price_override.clear_pin(user.id, payload.key)
        log.info("discord: price pin cleared for %s by %s", payload.key, user.id)
    else:
        try:
            value = price_override.set_pin(user.id, payload.key, raw)
        except ValueError as exc:
            raise HTTPException(400, f"Price {exc}.") from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        log.warning(
            "discord: PRICE PINNED %s = %s for user=%s — enforcement will act on this",
            payload.key, value, user.id,
        )
        # A simulated path advances every second. Evaluate its trim gate now
        # rather than making the tester wait up to the normal 15-second worker
        # cadence. The price-pin feature gate above keeps this test-only.
        from app.services import discord_auto_trim  # noqa: PLC0415
        discord_auto_trim.tick(user.id)
    return list_simulated_prices(db, user, None, None)


@router.delete("/simulated-prices", status_code=status.HTTP_204_NO_CONTENT)
def clear_simulated_prices(
    user: User = Depends(require_trader),
    _f: None = Depends(_require_feature),
    _p: None = Depends(_require_pin_feature),
):
    # No `-> None` annotation: with `from __future__ import annotations`
    # FastAPI resolves it as a response model and rejects it on a 204.
    """Drop every pin this trader has — the way back to real prices."""
    n = price_override.clear_all(user.id)
    log.info("discord: cleared %d price pin(s) for user=%s", n, user.id)


@router.get("/signals/page", response_model=Page[DiscordSignalOut])
def list_signals(
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, description="Symbol substring"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[DiscordSignalOut]:
    """Parsed Discord alerts across ALL of this trader's sources, newest first.

    Powers the "Discord" tab in Order History. Display only — these are readings
    of what alerts said, not orders. Defaults to hiding non-trade chatter, since
    a channel is mostly chatter and an unfiltered list would bury the alerts.
    """
    joined = (
        select(DiscordMessage, DiscordAlertSource)
        .join(DiscordAlertSource, DiscordAlertSource.id == DiscordMessage.source_id)
        .where(DiscordMessage.user_id == user.id)
    )
    if status_filter and status_filter != "all":
        try:
            joined = joined.where(DiscordMessage.status == DiscordMessageStatus(status_filter))
        except ValueError:
            raise HTTPException(400, f"invalid_status: {status_filter}")
    else:
        # "All" still means all TRADE-ish messages — plain chatter isn't an
        # order-history row.
        joined = joined.where(
            DiscordMessage.status != DiscordMessageStatus.IGNORED
        )
    if search:
        joined = joined.where(DiscordMessage.content.ilike(f"%{search}%"))

    total = db.execute(
        select(func.count()).select_from(joined.subquery())
    ).scalar_one()
    rows = db.execute(
        joined.order_by(DiscordMessage.created_at.desc()).limit(limit).offset(offset)
    ).all()

    # One ROW PER TRADE, not per message: a message carrying two exits is two
    # rows, because that's what the trader needs to see. `total` stays a message
    # count — paging on an expanded count would need a different query shape and
    # the discrepancy is only visible on multi-trade alerts.
    # Sizing is account-wide, so read it once rather than per row.
    from app.models.settings import TraderSettings  # noqa: PLC0415 — avoid a cycle

    ts = db.get(TraderSettings, user.id)
    multiplier = (ts.discord_quantity_multiplier if ts else 1) or 1

    # Actual order quantities for rows that already placed one — the real figure
    # beats any recomputation.
    order_ids = [m.order_id for m, _ in rows if m.order_id]
    placed: dict = {}
    if order_ids:
        placed = {
            o.id: o.quantity
            for o in db.execute(select(Order).where(Order.id.in_(order_ids))).scalars()
        }

    items: list[DiscordSignalOut] = []
    for m, src in rows:
        signals = list(m.parsed_signals or ([m.parsed_signal] if m.parsed_signal else []))
        if not signals:
            items.append(_signal_out(m, src, {}, 0, multiplier, placed))
            continue
        for idx, sig in enumerate(signals):
            items.append(_signal_out(m, src, sig or {}, idx, multiplier, placed))

    return Page[DiscordSignalOut](
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


def _effective_quantity(m: DiscordMessage, sig: dict, multiplier: int, placed: dict) -> str | None:
    """What will actually be traded, as distinct from what the alert said.

    Order of preference: the real order if one was placed, then the alert's size
    scaled by the multiplier. A close returns None — its size comes from the
    position held, which isn't knowable here.
    """
    if m.order_id and m.order_id in placed:
        return str(placed[m.order_id])
    if (sig.get("action") or "").upper() == "SELL":
        return None
    raw = sig.get("quantity")
    if raw in (None, ""):
        return None
    try:
        return str(int(Decimal(str(raw)) * max(1, multiplier)))
    except (InvalidOperation, ValueError):
        return None


def _signal_out(
    m: DiscordMessage, src: DiscordAlertSource, sig: dict, idx: int = 0,
    multiplier: int = 1, placed: dict | None = None,
) -> DiscordSignalOut:
    """Flatten a message + ONE of its parsed signals into a table row."""
    return DiscordSignalOut(
        # A message with several trades produces several rows, so the row key
        # has to distinguish them.
        row_key=f"{m.id}:{idx}",
        id=m.id,
        source_id=src.id,
        source_label=src.label,
        channel_name=src.channel_name,
        discord_message_id=m.discord_message_id,
        author=m.author,
        posted_at=m.posted_at,
        created_at=m.created_at,
        content=m.content or "",
        embeds=list(m.embeds or []),
        status=m.status.value if hasattr(m.status, "value") else str(m.status),
        status_reason=m.status_reason,
        decision=(m.decision.value if m.decision else None),
        decided_at=m.decided_at,
        decision_mode=m.decision_mode,
        action=sig.get("action"),
        asset_type=sig.get("asset_type"),
        symbol=sig.get("symbol"),
        option_type=sig.get("option_type"),
        strike=sig.get("strike"),
        expiration=sig.get("expiration"),
        quantity=sig.get("quantity"),
        effective_quantity=_effective_quantity(m, sig, multiplier, placed or {}),
        order_type=sig.get("order_type"),
        limit_price=sig.get("limit_price"),
        is_partial_close=bool(sig.get("is_partial_close")),
        remaining_quantity=sig.get("remaining_quantity"),
        original_quantity=sig.get("original_quantity"),
        position_closed=bool(sig.get("position_closed")),
        source_action=sig.get("source_action"),
        notional=sig.get("notional"),
        pnl_amount=sig.get("pnl_amount"),
        pnl_percent=sig.get("pnl_percent"),
        total_pnl_amount=sig.get("total_pnl_amount"),
        total_pnl_percent=sig.get("total_pnl_percent"),
        order_id=m.order_id,
        expiry_unspecified=bool(sig.get("expiry_unspecified")),
        contract_unspecified=bool(sig.get("contract_unspecified")),
        limit_price_unspecified=bool(sig.get("limit_price_unspecified")),
    )


# NOTE: registered BEFORE "/{source_id}" on purpose — FastAPI matches in
# registration order, and a literal segment must win over the UUID converter.
def _settings_out(ts) -> DiscordSettingsOut:
    """The Alert handling values in ``ts`` (account row or a channel's own)."""
    return DiscordSettingsOut(
        execution_mode=(
            "auto" if ts is not None and (ts.discord_execution_mode or "manual").lower() == "auto"
            else "manual"
        ),
        live_trading=bool(ts and ts.discord_live_trading),
        auto_trim=bool(ts and getattr(ts, "discord_auto_trim", False)),
        exit_mode=discord_channel_settings.exit_mode(ts),
        quantity_multiplier=(ts.discord_quantity_multiplier if ts else 1) or 1,
        size_mode=(getattr(ts, "discord_size_mode", None) or "contracts") if ts else "contracts",
        size_dollars=_plain(getattr(ts, "discord_size_dollars", None)) if ts else None,
        size_cap=discord_channel_settings.active_sizing(ts)["cap"] if ts else "none",
        max_per_contract=_plain(ts.discord_max_per_contract) if ts else None,
        max_per_order=_plain(ts.discord_max_per_order) if ts else None,
        trail_percent=(_plain(ts.discord_trail_percent) if ts else "20") or "20",
        trim_profit_gate_pct=_plain(_setting(ts, "discord_trim_profit_gate_pct", "20")),
        trim_stop_pct=_plain(_setting(ts, "discord_trim_stop_pct", "-25")),
        trim2_profit_gate_pct=_plain(_setting(ts, "discord_trim2_profit_gate_pct", "0")),
        trim2_stop_pct=_plain(_setting(ts, "discord_trim2_stop_pct", "0")),
        trim3_profit_gate_pct=_plain(_setting(ts, "discord_trim3_profit_gate_pct", "0")),
        trim3_stop_pct=_plain(_setting(ts, "discord_trim3_stop_pct", "0")),
        trim_qty_pct=_plain(_setting(ts, "discord_trim_qty_pct", "50")),
        trim2_qty_pct=_plain(_setting(ts, "discord_trim2_qty_pct", "50")),
        trim3_qty_pct=_plain(_setting(ts, "discord_trim3_qty_pct", "100")),
        trim_price_threshold=_plain(_setting(ts, "discord_trim_price_threshold", "0.90")),
        trim_trail_amount=_plain(_setting(ts, "discord_trim_trail_amount", "0.25")),
        trims=[
            DiscordTrimRow(profit_gate_pct=_plain(r.profit_gate_pct), qty_pct=_plain(r.qty_pct),
                           stop_pct=_plain(r.stop_pct), stop_trail=r.stop_trail)
            for r in discord_ladder.rungs(ts)
        ],
        fill_stop_pct=_plain(discord_ladder.fill_stop_pct(ts)),
        fill_stop_trail=discord_ladder.fill_stop_trails(ts),
        reprice_after_seconds=(
            getattr(ts, "discord_reprice_after_seconds", None) or 30 if ts else 30
        ),
        reprice_pct=_plain(_setting(ts, "discord_reprice_pct", "10")),
    )


def _apply_settings(ts, payload: DiscordSettingsIn, user: User) -> None:
    """Validate and apply a settings PATCH onto ``ts`` — the account's row, or a
    channel's own values (discord_channel_settings.ChannelSettings), which read
    and write exactly like the row. Caller commits."""
    if payload.execution_mode is not None:
        ts.discord_execution_mode = payload.execution_mode
    if payload.quantity_multiplier is not None:
        ts.discord_quantity_multiplier = payload.quantity_multiplier
    if "size_dollars" in payload.model_fields_set:
        raw = (payload.size_dollars or "").strip().lstrip("$").replace(",", "")
        if raw == "":
            ts.discord_size_dollars = None
        else:
            try:
                amount = Decimal(raw)
            except (InvalidOperation, ValueError):
                raise HTTPException(400, "Dollars per entry is not a number")
            if not amount.is_finite() or amount < 1 or amount > 1_000_000:
                raise HTTPException(400, "Dollars per entry must be between $1 and $1,000,000")
            ts.discord_size_dollars = amount.quantize(Decimal("0.01"))
    if payload.size_mode is not None:
        if payload.size_mode == "dollars" and not getattr(ts, "discord_size_dollars", None):
            raise HTTPException(400, "Set the dollars per entry first")
        ts.discord_size_mode = payload.size_mode
    if payload.size_cap is not None:
        ts.discord_size_cap = payload.size_cap
    if payload.max_per_contract is not None:
        raw = payload.max_per_contract.strip()
        if not raw:
            ts.discord_max_per_contract = None       # cleared
        else:
            try:
                value = Decimal(raw)
            except (InvalidOperation, ValueError):
                raise HTTPException(400, "invalid_max_per_contract")
            if value <= 0:
                raise HTTPException(400, "max_per_contract must be positive")
            ts.discord_max_per_contract = value
    if payload.max_per_order is not None:
        raw = payload.max_per_order.strip()
        if not raw:
            ts.discord_max_per_order = None          # cleared
        else:
            try:
                value = Decimal(raw)
            except (InvalidOperation, ValueError):
                raise HTTPException(400, "invalid_max_per_order")
            if value <= 0:
                raise HTTPException(400, "max_per_order must be positive")
            ts.discord_max_per_order = value
    if payload.trail_percent is not None:
        try:
            trail = Decimal(payload.trail_percent.strip())
        except (InvalidOperation, ValueError, AttributeError):
            raise HTTPException(400, "invalid_trail_percent")
        # A zero/negative trail would never trigger; above 100 is meaningless.
        if not (0 < trail <= 100):
            raise HTTPException(400, "trail_percent must be between 0 and 100")
        ts.discord_trail_percent = trail
    # Ladder thresholds. Each is a positive number; the two percentages are
    # additionally capped at 100, where they stop meaning anything.
    # The ladder percentages accept 0, the other thresholds do not: a gate of 0
    # means "no minimum profit" and a stop of 0 means break-even, both of which
    # a trader can legitimately want. A price threshold or trail of 0, by
    # contrast, is not a setting — it is an empty field.
    _ZERO_OK = Decimal(0)
    _POSITIVE = None
    # A stop may be typed the way a trader says it out loud — "-25%", meaning
    # 25% below entry. The sign is how people WRITE a drawdown; the ladder
    # reads the distance, so -25 and 25 set the same level. Floored at -100
    # rather than 0 purely so the spelling is accepted.
    _SIGNED = Decimal(-100)
    for field, column, cap, floor in (
        ("trim_profit_gate_pct", "discord_trim_profit_gate_pct", Decimal(100), _ZERO_OK),
        ("trim_stop_pct", "discord_trim_stop_pct", Decimal(100), _SIGNED),
        ("trim2_profit_gate_pct", "discord_trim2_profit_gate_pct", Decimal(100), _ZERO_OK),
        ("trim2_stop_pct", "discord_trim2_stop_pct", Decimal(100), _SIGNED),
        ("trim3_profit_gate_pct", "discord_trim3_profit_gate_pct", Decimal(100), _ZERO_OK),
        ("trim3_stop_pct", "discord_trim3_stop_pct", Decimal(100), _SIGNED),
        # A rung's share of what is still held. Capped at 100 (a rung cannot
        # sell more than the position) and floored at 0, where 0 means the rung
        # sells nothing — which is a legitimate way to switch a rung off.
        ("trim_qty_pct", "discord_trim_qty_pct", Decimal(100), _ZERO_OK),
        ("trim2_qty_pct", "discord_trim2_qty_pct", Decimal(100), _ZERO_OK),
        ("trim3_qty_pct", "discord_trim3_qty_pct", Decimal(100), _ZERO_OK),
        ("trim_price_threshold", "discord_trim_price_threshold", None, _POSITIVE),
        ("trim_trail_amount", "discord_trim_trail_amount", None, _POSITIVE),
        ("reprice_pct", "discord_reprice_pct", Decimal(100), _POSITIVE),
    ):
        raw = getattr(payload, field, None)
        if raw is None:
            continue
        try:
            value = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError, AttributeError):
            raise HTTPException(400, f"invalid_{field}")
        too_small = value < floor if floor is not None else value <= 0
        if too_small or (cap is not None and value > cap):
            raise HTTPException(
                400,
                f"{field} must be "
                + ("0 or more" if floor is not None else "greater than 0")
                + (f" and no more than {cap}" if cap is not None else ""),
            )
        setattr(ts, column, value)

    if payload.reprice_after_seconds is not None:
        ts.discord_reprice_after_seconds = payload.reprice_after_seconds

    def _pct(raw, what: str, low: Decimal, high: Decimal) -> Decimal:
        try:
            value = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError, AttributeError):
            raise HTTPException(400, f"{what} is not a number")
        if not value.is_finite() or value < low or value > high:
            raise HTTPException(400, f"{what} must be between {low} and {high}")
        return value

    # The whole ladder at once: any number of trims, in order. After the
    # per-trim fields above, so it wins when both are sent.
    def _trail_pct(raw, what: str) -> Decimal:
        """A trailing stop's give-back: 15 (or "-15", as people write a drop) is
        15% below the high. Stored positive."""
        value = abs(_pct(raw, what, Decimal(-99), Decimal(99)))
        if value < 1:
            raise HTTPException(400, f"{what} trails by a give-back between 1 and 99%")
        return value

    if payload.trims is not None:
        discord_ladder.store(ts, [
            (
                _pct(t.profit_gate_pct, f"Trim {i} profit target", Decimal(0), Decimal(1000)),
                (_trail_pct(t.stop_pct, f"Trim {i} trailing stop") if t.stop_trail
                 else _pct(t.stop_pct, f"Trim {i} stop", Decimal(-100), Decimal(1000))),
                _pct(t.qty_pct, f"Trim {i} quantity", Decimal(0), Decimal(100)),
            )
            for i, t in enumerate(payload.trims, 1)
        ], [t.stop_trail for t in payload.trims])
        log.info("discord: ladder set to %d trim(s) for user %s", len(payload.trims), user.id)

    # "On Fill" stop. Sent as "" to clear — so "not sent" and "cleared" differ.
    if "fill_stop_pct" in payload.model_fields_set:
        raw = (payload.fill_stop_pct or "").strip()
        trailing = bool(payload.fill_stop_trail)
        if raw == "":
            ts.discord_fill_stop_pct = None
            discord_ladder.store_fill_trail(ts, False)
        elif trailing:
            ts.discord_fill_stop_pct = _trail_pct(raw, "On Fill trailing stop")
            discord_ladder.store_fill_trail(ts, True)
        else:
            discord_ladder.store_fill_trail(ts, False)
            value = _pct(raw, "On Fill stop", Decimal(-99), Decimal(1000))
            if value >= 0:
                # At fill the price IS the entry, so a stop at or above it is
                # already through the market and would sell the position at once.
                raise HTTPException(
                    400, "The On Fill stop sits below entry — enter it as a negative, like -25.")
            ts.discord_fill_stop_pct = value

    if payload.auto_trim is not None:
        ts.discord_auto_trim = payload.auto_trim
        if payload.auto_trim:
            ts.discord_manual_exit = False     # auto-trim on means exits are not manual
            ts.discord_tp_orders = False       # …and not resting take-profit orders
        log.info(
            "discord: auto-trim %s for user %s",
            "ENABLED" if payload.auto_trim else "disabled", user.id,
        )

    # The three-way choice: wait for the alert, auto-trim, or leave it to the
    # trader. Applied after auto_trim so it wins when both are sent.
    if payload.exit_mode is not None:
        ts.discord_auto_trim = payload.exit_mode == "auto"
        ts.discord_manual_exit = payload.exit_mode == "manual"
        ts.discord_tp_orders = payload.exit_mode == "orders"
        log.info("discord: exits set to %s for user %s", payload.exit_mode, user.id)

    if payload.live_trading is not None:
        ts.discord_live_trading = payload.live_trading
        log.warning(
            "discord: LIVE TRADING %s for user=%s",
            "ENABLED" if payload.live_trading else "disabled", user.id,
        )


@router.get("/settings", response_model=DiscordSettingsOut)
def get_discord_settings(
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> DiscordSettingsOut:
    """Account-wide handling of inbound Discord alerts."""
    from app.models.settings import TraderSettings  # noqa: PLC0415 — avoid a cycle

    return _settings_out(db.get(TraderSettings, user.id))


@router.patch("/settings", response_model=DiscordSettingsOut)
def update_discord_settings(
    payload: DiscordSettingsIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> DiscordSettingsOut:
    """Account-wide Alert handling. Applies to alerts arriving from now on, on
    every channel that follows the account. Decisions already recorded keep the
    mode that applied at the time — switching to auto must not retroactively
    approve alerts the trader never saw.
    """
    from app.models.settings import TraderSettings  # noqa: PLC0415 — avoid a cycle

    ts = db.get(TraderSettings, user.id)
    if ts is None:
        ts = TraderSettings(user_id=user.id)
        db.add(ts)
    _apply_settings(ts, payload, user)
    db.commit()
    return _settings_out(ts)


# ── Per-channel alert handling ───────────────────────────────────────────────
# A channel follows the account's settings until its "Use account settings"
# switch is turned off; then it has its own copy (services/discord_channel_settings).

def _channel_settings_out(db: Session, user: User, src: DiscordAlertSource) -> ChannelSettingsOut:
    from app.models.settings import TraderSettings  # noqa: PLC0415

    eff = discord_channel_settings.effective(db, user.id, src.id)
    base = _settings_out(eff if eff is not None else db.get(TraderSettings, user.id))
    return ChannelSettingsOut(
        **base.model_dump(),
        use_account_settings=bool(src.use_account_settings),
        entry_order_type=discord_channel_settings.entry_order_type(db, src.id),
    )


@router.get("/{source_id}/settings", response_model=ChannelSettingsOut)
def get_channel_settings(
    source_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> ChannelSettingsOut:
    """This channel's Alert handling: the account's values while it follows the
    account, else its own."""
    return _channel_settings_out(db, user, _get_owned(db, user, source_id))


@router.patch("/{source_id}/settings", response_model=ChannelSettingsOut)
def update_channel_settings(
    source_id: uuid.UUID,
    payload: ChannelSettingsIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> ChannelSettingsOut:
    """Turn "Use account settings" off (the channel gets a copy of the account's
    values) or on (back to the account's), set Market / Limit entries, or edit
    the channel's own values."""
    from app.models.settings import TraderSettings  # noqa: PLC0415

    src = _get_owned(db, user, source_id)
    if payload.entry_order_type is not None:
        src.entry_order_type = payload.entry_order_type
    if payload.use_account_settings is not None and payload.use_account_settings != src.use_account_settings:
        if not payload.use_account_settings:
            # Start from the account's values as they are right now.
            src.channel_settings = discord_channel_settings.snapshot(db.get(TraderSettings, user.id))
        src.use_account_settings = payload.use_account_settings
    edits = DiscordSettingsIn(**payload.model_dump(
        exclude={"use_account_settings", "entry_order_type"}, exclude_unset=True,
    ))
    if edits.model_dump(exclude_none=True):
        if src.use_account_settings:
            raise HTTPException(
                409, "This channel uses the account settings — turn that off to edit its own."
            )
        own = discord_channel_settings.ChannelSettings(
            db.get(TraderSettings, user.id), src.channel_settings or {},
        )
        _apply_settings(own, edits, user)
        src.channel_settings = own.to_json()     # reassign so the JSON change is saved
    db.commit()
    return _channel_settings_out(db, user, src)


# ── AI trimming ──────────────────────────────────────────────────────────────
# The alternative exit engine (services/ai_trim.py). Its own endpoints rather
# than more fields on /settings: the two engines are configured on separate
# tabs, and the decision log has actions of its own.


class AiTrimSettingsOut(BaseModel):
    engine: str                 # ladder | ai
    mode: str                   # suggest | auto
    model: str
    move_pct: str
    min_interval_s: int
    instructions: str
    key_configured: bool        # OPENROUTER_API_KEY is set on the server
    live_trading: bool          # Discord live trading — AI orders are paper without it


class AiTrimSettingsIn(BaseModel):
    engine: str | None = Field(default=None, pattern=r"^(ladder|ai)$")
    mode: str | None = Field(default=None, pattern=r"^(suggest|auto)$")
    model: str | None = Field(default=None, min_length=3, max_length=120)
    move_pct: Decimal | None = Field(default=None, gt=0, le=100)
    min_interval_s: int | None = Field(default=None, ge=15, le=3600)
    instructions: str | None = Field(default=None, max_length=2000)


class AiTrimDecisionOut(BaseModel):
    id: str
    contract: str
    model: str
    mode: str
    mark: str
    entry_price: str | None = None
    held: str
    action: str
    sell_qty: str
    new_stop_price: str | None = None
    reason: str
    notes: str | None = None
    status: str
    order_id: str | None = None
    created_at: datetime


def _ai_settings_out(ts) -> AiTrimSettingsOut:
    return AiTrimSettingsOut(
        engine=getattr(ts, "discord_exit_engine", None) or "ladder",
        mode=getattr(ts, "discord_ai_mode", None) or "suggest",
        model=getattr(ts, "discord_ai_model", None) or "anthropic/claude-sonnet-5.5",
        move_pct=_plain(_setting(ts, "discord_ai_move_pct", "5")) or "5",
        min_interval_s=getattr(ts, "discord_ai_min_interval_s", None) or 60,
        instructions=getattr(ts, "discord_ai_instructions", None) or "",
        key_configured=bool(get_settings().openrouter_api_key),
        live_trading=bool(ts is not None and ts.discord_live_trading),
    )


def _ai_decision_out(row) -> AiTrimDecisionOut:
    return AiTrimDecisionOut(
        id=str(row.id), contract=row.contract, model=row.model, mode=row.mode,
        mark=_plain(row.mark) or "0", entry_price=_plain(row.entry_price),
        held=_plain(row.held) or "0", action=row.action,
        sell_qty=_plain(row.sell_qty) or "0", new_stop_price=_plain(row.new_stop_price),
        reason=row.reason, notes=row.notes, status=row.status,
        order_id=str(row.order_id) if row.order_id else None,
        created_at=row.created_at,
    )


@router.get("/ai-trim", response_model=AiTrimSettingsOut)
def get_ai_trim_settings(
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> AiTrimSettingsOut:
    from app.models.settings import TraderSettings  # noqa: PLC0415

    return _ai_settings_out(db.get(TraderSettings, user.id))


@router.patch("/ai-trim", response_model=AiTrimSettingsOut)
def update_ai_trim_settings(
    payload: AiTrimSettingsIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> AiTrimSettingsOut:
    from app.models.settings import TraderSettings  # noqa: PLC0415

    ts = db.get(TraderSettings, user.id)
    if ts is None:
        ts = TraderSettings(user_id=user.id)
        db.add(ts)
    if payload.engine is not None:
        ts.discord_exit_engine = payload.engine
        log.warning("discord: exit engine -> %s for user=%s", payload.engine.upper(), user.id)
    if payload.mode is not None:
        ts.discord_ai_mode = payload.mode
        log.warning("discord: AI trimming mode -> %s for user=%s", payload.mode.upper(), user.id)
    if payload.model is not None:
        ts.discord_ai_model = payload.model.strip()
    if payload.move_pct is not None:
        ts.discord_ai_move_pct = payload.move_pct
    if payload.min_interval_s is not None:
        ts.discord_ai_min_interval_s = payload.min_interval_s
    if payload.instructions is not None:
        ts.discord_ai_instructions = payload.instructions.strip() or None
    db.commit()
    return _ai_settings_out(ts)


@router.get("/ai-trim/decisions", response_model=list[AiTrimDecisionOut])
def list_ai_trim_decisions(
    limit: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> list[AiTrimDecisionOut]:
    from app.models.ai_trim_decision import AiTrimDecision  # noqa: PLC0415
    from app.services import ai_trim  # noqa: PLC0415

    rows = list(db.execute(
        select(AiTrimDecision).where(AiTrimDecision.user_id == user.id)
        .order_by(AiTrimDecision.created_at.desc()).limit(limit)
    ).scalars())
    # Age out suggestions nobody acted on, so the list never offers one whose
    # price is long gone. Approval checks this too; this keeps the view honest.
    now = datetime.now(timezone.utc)
    stale = [r for r in rows if r.status == "suggested" and now - r.created_at > ai_trim.SUGGESTION_TTL]
    for r in stale:
        r.status = "expired"
    if stale:
        db.commit()
    return [_ai_decision_out(r) for r in rows]


def _owned_decision(db: Session, user: User, decision_id: uuid.UUID):
    from app.models.ai_trim_decision import AiTrimDecision  # noqa: PLC0415

    row = db.get(AiTrimDecision, decision_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "No such decision.")
    return row


@router.post("/ai-trim/decisions/{decision_id}/approve", response_model=AiTrimDecisionOut)
def approve_ai_trim_decision(
    decision_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> AiTrimDecisionOut:
    """Carry out a suggestion — re-validated against the position as it is now."""
    from app.brokers import adapter_for  # noqa: PLC0415
    from app.models.broker_account import BrokerAccount  # noqa: PLC0415
    from app.models.settings import TraderSettings  # noqa: PLC0415
    from app.services import ai_trim, discord_auto_trim  # noqa: PLC0415
    from app.services.crypto import decrypt_json  # noqa: PLC0415

    row = _owned_decision(db, user, decision_id)
    acct = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().first()
    if acct is None:
        raise HTTPException(409, "No connected broker account.")
    adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
    try:
        # Not the cached read: this one decides what gets sold.
        positions = adapter.get_positions()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Couldn't read positions: {exc}") from exc
    try:
        ai_trim.approve(
            db, user, row, positions, adapter, acct, db.get(TraderSettings, user.id),
            lambda ps, g: discord_auto_trim._mark_for(ps, g, user.id),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _ai_decision_out(row)


@router.post("/ai-trim/decisions/{decision_id}/dismiss", response_model=AiTrimDecisionOut)
def dismiss_ai_trim_decision(
    decision_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> AiTrimDecisionOut:
    row = _owned_decision(db, user, decision_id)
    if row.status != "suggested":
        raise HTTPException(409, f"This decision is {row.status}, not waiting for approval.")
    row.status = "dismissed"
    row.decided_at = datetime.now(timezone.utc)
    db.commit()
    return _ai_decision_out(row)


class AiModelOut(BaseModel):
    id: str
    name: str
    prompt_per_m: str | None = None       # USD per million input tokens
    completion_per_m: str | None = None   # USD per million output tokens


@router.get("/ai-trim/models", response_model=list[AiModelOut])
def list_ai_trim_models(
    user: User = Depends(require_discord_member),
) -> list[AiModelOut]:
    """Models the AI trimming engine can use (they must support structured output)."""
    from app.services import ai_trim  # noqa: PLC0415

    try:
        return [AiModelOut(**m) for m in ai_trim.list_models()]
    except ai_trim.ModelError as exc:
        raise HTTPException(502, str(exc)) from exc


class AiTrimTestOut(BaseModel):
    ok: bool
    model: str
    action: str | None = None
    reason: str | None = None
    error: str | None = None


@router.post("/ai-trim/test", response_model=AiTrimTestOut)
def test_ai_trim(
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> AiTrimTestOut:
    """One call against a made-up position: proves the key and the model work.
    Records nothing and trades nothing."""
    from types import SimpleNamespace  # noqa: PLC0415

    from app.models.settings import TraderSettings  # noqa: PLC0415
    from app.services import ai_trim, market_hours  # noqa: PLC0415

    ts = db.get(TraderSettings, user.id)
    settings = _ai_settings_out(ts)
    sample = SimpleNamespace(
        symbol="SPY", option_strike=Decimal("500"), option_right="call",
        option_expiry=market_hours.now_et().date() + timedelta(days=2),
        entry_price=Decimal("2.00"), stop_price=Decimal("1.50"),
    )
    try:
        raw = ai_trim.ask(settings.model, ai_trim.build_messages(
            ts, sample, Decimal("2.90"), Decimal("4"), Decimal("3.10"), [],
            datetime.now(timezone.utc),
        ))
    except ai_trim.ModelError as exc:
        return AiTrimTestOut(ok=False, model=settings.model, error=str(exc))
    d = ai_trim.validate(raw, Decimal("4"), Decimal("2.90"), Decimal("1.50"))
    return AiTrimTestOut(ok=True, model=settings.model, action=d.action, reason=d.reason)


@router.patch("/{source_id}", response_model=DiscordSourceOut)
def update_source(
    source_id: uuid.UUID,
    payload: DiscordSourceUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> DiscordSourceOut:
    src = _get_owned(db, user, source_id)
    if src.parent_source_id is not None or user.role != UserRole.TRADER:
        # A subscriber's copy of a trader channel: the switch is theirs, the
        # channel itself (name, URL, schedule, parsing) is the trader's.
        changed = payload.model_dump(exclude_unset=True, exclude_none=True)
        if set(changed) - {"is_enabled"}:
            raise HTTPException(403, "Only the on/off switch can be changed on a trader's channel.")
        if payload.is_enabled is not None:
            src.is_enabled = payload.is_enabled
            src.paused_by_pause_all = False      # set by hand now
        db.commit()
        parent = (db.get(DiscordAlertSource, src.parent_source_id)
                  if src.parent_source_id else None)
        if parent is not None:
            return _mirror_out(src, parent)
        out = _to_out(src)
        out.mirrored = True
        return out
    if payload.label is not None:
        src.label = payload.label.strip()
    if payload.percent_means_exit is not None:
        src.percent_means_exit = payload.percent_means_exit
        log.info(
            "discord: source %s percent_means_exit=%s",
            src.id, payload.percent_means_exit,
        )
    if payload.channel_url is not None:
        try:
            guild_id, channel_id = parse_channel_url(payload.channel_url)
        except DiscordSessionError as exc:
            raise HTTPException(400, f"invalid_channel_url: {exc}")
        if channel_id != src.channel_id:
            src.guild_id = guild_id
            src.channel_id = channel_id
            # Names describe the OLD channel; the listener backfills them once
            # it has the new one open.
            src.channel_name = None
            src.guild_name = None
            # The high-water mark is per-channel. Carrying it over would make the
            # new channel's backlog look already-ingested (snowflakes are
            # globally time-ordered), silently suppressing real alerts.
            src.last_seen_message_id = None
            src.last_message_at = None
            src.last_error = None
            # The session lives on the account, so repointing a channel never
            # costs a sign-in.
            has_session = bool(src.account and src.account.encrypted_session)
            src.status = (
                "connecting" if (has_session and src.is_enabled) else
                "disconnected" if not src.is_enabled else "needs_login"
            )
    if payload.schedule_mode is not None:
        src.schedule_mode = payload.schedule_mode
    if payload.schedule_start is not None:
        src.schedule_start = payload.schedule_start
    if payload.schedule_end is not None:
        src.schedule_end = payload.schedule_end
    if payload.schedule_timezone is not None:
        src.schedule_timezone = payload.schedule_timezone or None
    if payload.schedule_days is not None:
        # Ignore junk rather than reject: an out-of-range day would otherwise
        # make the whole window unsatisfiable and silently stop alerts.
        src.schedule_days = sorted({d for d in payload.schedule_days if 0 <= d <= 6})
    if payload.is_enabled is not None:
        src.paused_by_pause_all = False          # set by hand now
    if payload.is_enabled is not None and payload.is_enabled != src.is_enabled:
        src.is_enabled = payload.is_enabled
        # Reflect the intent immediately so the UI doesn't show a stale
        # 'connected' pill for a source the listener is about to drop. The
        # listener reconciles within its poll interval and writes the real state.
        if not payload.is_enabled:
            src.status = "disconnected"
        elif src.account and src.account.encrypted_session:
            src.status = "connecting"
        else:
            src.status = "needs_login"
    try:
        db.commit()
    except IntegrityError:
        # uq_discord_source_user_channel — repointed onto a channel this trader
        # already watches elsewhere.
        db.rollback()
        raise HTTPException(409, "channel_already_connected")
    db.refresh(src)
    return _to_out(src)


@router.put("/{source_id}/session", response_model=DiscordSourceOut)
def upload_session(
    source_id: uuid.UUID,
    payload: DiscordSessionIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
) -> DiscordSourceOut:
    """Store the Discord Web session captured by the login helper.

    Kopyya never performs the login: the trader signs in themselves in a real
    browser window, typing their password and MFA code into Discord. What
    arrives here is only the resulting storage state, which we validate for
    shape and encrypt before it touches the database.
    """
    src = _get_owned(db, user, source_id)
    try:
        state = validate_storage_state(payload.storage_state)
    except DiscordSessionError as exc:
        raise HTTPException(400, f"invalid_session: {exc}")

    if _store_session(db, src, state) is None:
        raise HTTPException(409, "source_has_no_account")
    db.commit()
    db.refresh(src)
    log.info("discord: session stored on account for source=%s user=%s", src.id, user.id)
    return _to_out(src)


@router.post("/{source_id}/login", response_model=DiscordLoginOut, status_code=status.HTTP_201_CREATED)
def start_login(
    source_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
) -> DiscordLoginOut:
    """Begin a QR login for this source.

    The listener picks the request up on its next poll, opens Discord's own login
    page and captures the QR. The trader scans it with the Discord mobile app and
    approves on their phone — their password and MFA never touch Kopyya.
    """
    src = _get_owned(db, user, source_id)
    # Refuse to hammer Discord's login page. Back-to-back attempts are what earn
    # a browser an anti-bot challenge instead of a QR, and we would rather tell
    # the trader to wait a moment than spend five minutes failing.
    wait = discord_login.cooldown_remaining(src.id)
    if wait:
        raise HTTPException(
            429, f"login_cooldown: please wait {wait}s before trying again"
        )
    session = discord_login.create(src.id, user.id)
    log.info("discord: QR login started for source=%s user=%s", src.id, user.id)
    return DiscordLoginOut(
        session_id=session["session_id"], status=session["status"]
    )


@router.get("/{source_id}/login/{session_id}", response_model=DiscordLoginOut)
def poll_login(
    source_id: uuid.UUID,
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
) -> DiscordLoginOut:
    """Current state of a login attempt, including the live QR to display.

    Ownership is checked twice on purpose: the source must belong to the caller,
    AND the session must belong to that source. The QR is a scannable credential —
    handing one to the wrong account would let an attacker capture the session of
    whoever scanned it.
    """
    src = _get_owned(db, user, source_id)
    session = discord_login.get(session_id)
    if session is None or session["source_id"] != str(src.id):
        raise HTTPException(404, "login_session_not_found")

    qr = session.get("qr_png")
    return DiscordLoginOut(
        session_id=session["session_id"],
        status=session["status"],
        qr_image=(f"data:image/png;base64,{qr}" if qr else None),
        error=session.get("error"),
    )


@router.delete("/{source_id}/login/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_login(
    source_id: uuid.UUID,
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
):
    """Abandon a login attempt — closing the dialog shouldn't leave a live QR
    sitting in Redis waiting to be scanned."""
    src = _get_owned(db, user, source_id)
    session = discord_login.get(session_id)
    if session is not None and session["source_id"] == str(src.id):
        discord_login.finish(session_id, error="Cancelled.")


@router.post("/{source_id}/pair", response_model=DiscordPairOut, status_code=status.HTTP_201_CREATED)
def start_pairing(
    source_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
) -> DiscordPairOut:
    """Mint a pairing code for the Kopyya Connector desktop app.

    The trader reads this code off their screen and types it into the Connector,
    which signs them into Discord in a real browser on their own machine and
    uploads the resulting session. That keeps the capture off our servers, where
    Discord challenges automated logins.
    """
    src = _get_owned(db, user, source_id)
    session = discord_pairing.create(src.id, user.id)
    log.info("discord: pairing code issued for source=%s user=%s", src.id, user.id)
    return DiscordPairOut(
        code=discord_pairing.format_code(session["code"]), status=session["status"]
    )


@router.get("/{source_id}/pair/{code}", response_model=DiscordPairOut)
def poll_pairing(
    source_id: uuid.UUID,
    code: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
) -> DiscordPairOut:
    """Progress of a pairing, so the UI can flip to Connected on its own."""
    src = _get_owned(db, user, source_id)
    session = discord_pairing.get(code)
    if session is None or session["source_id"] != str(src.id):
        raise HTTPException(404, "pairing_not_found")
    return DiscordPairOut(
        code=discord_pairing.format_code(session["code"]),
        status=session["status"],
        error=session.get("error"),
    )


@router.delete("/{source_id}/session", response_model=DiscordSourceOut)
def clear_session(
    source_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
) -> DiscordSourceOut:
    """Forget the stored Discord session without deleting the source.

    The trader's way to revoke our access to their Discord account while keeping
    the channel configured. The listener drops the source on its next reconcile
    because the assignment disappears.
    """
    src = _get_owned(db, user, source_id)
    # Signing out revokes the ACCOUNT's session, so every channel read with it
    # goes offline together — anything else would imply per-channel logins that
    # no longer exist.
    acct = src.account
    if acct is not None:
        acct.encrypted_session = None
        acct.session_captured_at = None
        acct.status = "needs_login"
        for sibling in acct.sources:
            sibling.status = "needs_login"
    else:
        src.status = "needs_login"
    db.commit()
    db.refresh(src)
    return _to_out(src)


# ── the "Self" channel ──────────────────────────────────────────────────────
#
# A virtual source for alerts the trader submits by hand, when one was missed —
# the listener was down, the message scrolled past, the channel dropped. It is a
# real DiscordAlertSource row so that EVERYTHING downstream treats it like any
# other channel: the parser, the trim ladder, the sizing caps, the guards, the
# Channel column in Order History.
#
# What makes it virtual is the absence of an account. listener_assignments joins
# sources to DiscordAccount, so a source with account_id NULL is never handed to
# a watcher — there is no browser, no session, no channel to read. Nothing had
# to be excluded by name.


def _self_source(db: Session, user: User) -> DiscordAlertSource:
    """This trader's Self channel, created on demand.

    Keyed on the reserved channel_id, which the (user_id, channel_id) unique
    constraint then makes one-per-trader for free. A real Discord channel id is
    a numeric snowflake, so "self" cannot collide with one.
    """
    src = db.execute(
        select(DiscordAlertSource).where(
            DiscordAlertSource.user_id == user.id,
            DiscordAlertSource.channel_id == _SELF_CHANNEL_ID,
        )
    ).scalars().first()
    if src is not None:
        return src
    src = DiscordAlertSource(
        user_id=user.id,
        label=_SELF_LABEL,
        channel_id=_SELF_CHANNEL_ID,
        channel_name=_SELF_LABEL,
        # No account: this is what keeps the listener from ever trying to open
        # a watcher for it.
        account_id=None,
        is_enabled=True,
        # Always available. A schedule would mean "the trader may not replay a
        # missed alert right now", which is the opposite of the point.
        schedule_mode="always",
        status="connected",
    )
    db.add(src)
    db.flush()
    log.info("discord: opened the Self channel for user %s", user.id)
    return src


def _self_message_id() -> str:
    """A synthetic snowflake for a hand-submitted alert.

    Microseconds since the epoch. It has to be numeric and increasing because
    ingest orders sources by it (``_is_newer`` compares as int), and unique per
    source because that is the idempotency key. Two manual pastes cannot land in
    the same microsecond.
    """
    return str(int(time.time() * 1_000_000))


def _run_coroutine_inline(coro) -> None:  # noqa: ANN001
    """Run ``coro`` to completion from sync code on a worker thread."""
    import asyncio  # noqa: PLC0415

    from app.services import trade_listener  # noqa: PLC0415

    loop = trade_listener._main_loop
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is not None and loop.is_running() and running is not loop:
        asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=300)
    elif running is None:
        asyncio.run(coro)                      # no app loop: scripts, tests
    else:
        running.create_task(coro)              # on a loop thread: can't block it


class _InlineTasks(BackgroundTasks):
    """Run "background" work inline.

    _place_trader_order hands the subscriber fanout to BackgroundTasks, which a
    REQUEST drains after responding. A worker thread has nothing to drain it, so
    passing a plain BackgroundTasks would silently DROP the fanout — the trader
    would be trimmed and every subscriber left holding. Running it inline is
    correct here: we are already off the request path, so there is nothing to
    return to early.
    """

    def add_task(self, func, *args, **kwargs) -> None:  # noqa: ANN001, ANN003
        result = func(*args, **kwargs)
        if inspect.iscoroutine(result):
            # The fanout runner is async. Calling it only BUILT the coroutine —
            # it never ran, so every order placed off the request path (the
            # auto-trim sweep, pasted Self alerts) reached no subscriber. Run it
            # to completion on the app's main loop, where the fanout's
            # semaphores live (copy_engine.fanout_threadsafe).
            _run_coroutine_inline(result)


# "@Market" typed at the end of a composer alert: place the entry at market.
_AT_MARKET_RE = re.compile(r"\s*@market\s*$", re.IGNORECASE)


def _split_at_market(content: str) -> tuple[str, bool]:
    """(the alert without a trailing "@Market", whether it had one)."""
    text = (content or "").strip()
    stripped = _AT_MARKET_RE.sub("", text)
    return stripped, stripped != text


def _price_far_from_market(p) -> Decimal | None:
    """The live price, when the order's stated limit is nowhere near it (under
    half, or over double) — the alert's price is then not this instrument's.
    None when it is close, or no live price is to be had."""
    if p.limit_price is None or p.limit_price <= 0:
        return None
    try:
        from app.services import live_marks  # noqa: PLC0415

        live = live_marks.contract_mark(p.symbol, p.option_strike, p.option_right, p.option_expiry)
    except Exception:  # noqa: BLE001
        return None
    if live is None or live <= 0:
        return None
    limit = Decimal(str(p.limit_price))
    return live if (limit < live / 2 or limit > live * 2) else None


def submit_self_alert_text(
    db: Session,
    user: User,
    content: str,
    *,
    background: BackgroundTasks | None = None,
    request: Request | None = None,
    approve: bool = False,
    source_id: uuid.UUID | None = None,
) -> DiscordMessage | None:
    """Put ``content`` through the Discord pipeline as a Self-channel alert —
    or, with ``source_id``, as an alert of that channel of the user's (the
    composer's channel picker): that channel's settings size and manage it.

    The one place an alert can be injected without Discord — used by the
    composer in Order History and by auto-trim. Returns the stored message so
    the caller can report its verdict, or None if it could not be stored.

    ``approve`` forces execution past the trader's manual/auto gate. Auto-trim
    sets it: the trader turned auto-trim ON, which IS the approval, and an
    automatic trim that sat waiting for a second approval would miss the move
    it was watching for. The composer leaves it False, so a pasted alert behaves
    exactly as if Discord had delivered it.
    """
    src = _get_owned(db, user, source_id) if source_id is not None else _self_source(db, user)
    typed_into_channel = src.channel_id != _SELF_CHANNEL_ID
    # "... @Market" asks for this one alert's entry at market, whatever the
    # channel's own entry setting. Taken off before parsing (it is not part of
    # the alert) and carried on the parsed signal to execution.
    content, at_market = _split_at_market(content)
    # A typed alert is not the channel's watcher reporting in: it must not look
    # like a heartbeat (the disconnect watchdog reads these) or move the
    # channel's own "latest message" markers.
    watcher_state = (src.last_heartbeat_at, src.last_message_at, src.last_seen_message_id)
    raw = {
        "message_id": _self_message_id(),
        "channel_id": src.channel_id,
        "server_id": None,
        "author": user.email,
        "author_id": str(user.id),
        "content": (content or "").strip(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attachments": [],
        "embeds": [],
    }

    auto = approve or _auto_approve(db, user.id, src.id if typed_into_channel else None)
    report = discord_ingest.ingest_batch(db, src, [raw], auto_approve=auto)
    if at_market:
        for m in report.stored:
            if m.parsed_signal:
                m.parsed_signal = {**m.parsed_signal, "at_market": True}
                m.parsed_signals = [{**sig, "at_market": True} for sig in (m.parsed_signals or [m.parsed_signal])]
    if typed_into_channel:
        src.last_heartbeat_at, src.last_message_at, src.last_seen_message_id = watcher_state
    if not report.stored:
        return None

    msg = report.stored[0]
    if auto and msg.decision is SignalDecision.APPROVED:
        _execute_signal(db, user, msg, background or _InlineTasks(), request)
    db.commit()
    db.refresh(msg)
    return msg


@router.post("/self/alert", response_model=DiscordSelfAlertOut)
def submit_self_alert(
    payload: DiscordSelfAlertIn,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> DiscordSelfAlertOut:
    """Replay an alert the system missed, through the normal Discord pipeline.

    Reuses ``ingest_batch`` and ``_execute_signal`` verbatim — the same parse,
    the same auto/manual gate, the same sizing, caps, guards and fanout. There
    is deliberately no separate path: a second way into the order pipeline would
    be a second thing to keep correct, and it would be the one nobody tests.

    Honours the trader's execution mode, because that is what "as if Discord had
    delivered it" means. In auto it places; in manual it lands awaiting approval
    in the Discord tab, exactly where a real alert would have.
    """
    msg = submit_self_alert_text(
        db, user, payload.content, background=background, request=request,
        source_id=payload.source_id,
    )
    if msg is None:
        # ingest_batch only rejects a message with no id, which cannot happen
        # here — but returning a 500 on an impossible branch is worse than
        # saying plainly that nothing was stored.
        raise HTTPException(500, "could not store the alert")
    return DiscordSelfAlertOut(
        id=msg.id,
        content=msg.content or "",
        status=msg.status.value if hasattr(msg.status, "value") else str(msg.status),
        status_reason=msg.status_reason,
        decision=(msg.decision.value if msg.decision else None),
        order_id=msg.order_id,
        parsed_signal=msg.parsed_signal,
    )


def _alert_reason(db: Session, msg) -> str:
    """How the position summary explains what an alert did: the channel and
    what it said ("Mark: “TSLA -90% Out”"), or that it was typed by hand."""
    label = "Discord"
    try:
        src = db.get(DiscordAlertSource, msg.source_id) if getattr(msg, "source_id", None) else None
        if src is not None:
            label = "typed in the Discord popup" if src.channel_id == _SELF_CHANNEL_ID else src.label
    except Exception:  # noqa: BLE001 — a label is never worth failing an alert for
        pass
    text = " ".join(str(getattr(msg, "content", "") or "").split())
    text = text if len(text) <= 120 else text[:117] + "…"
    return f"{label} alert: “{text}”" if text else f"{label} alert"


def _with_alert_reason(fn):
    """Everything an alert does — its order, the stop it moves — is recorded in
    the position summary as caused by that alert. An outer reason (auto-trim,
    which submits its own alert) is kept."""
    import functools  # noqa: PLC0415

    @functools.wraps(fn)
    def _w(db, user, msg, background, request):
        from app.services import position_events  # noqa: PLC0415

        with position_events.because(_alert_reason(db, msg), keep_outer=True):
            result = fn(db, user, msg, background, request)
            # Save what the alert changed WHILE its reason is still set: the
            # caller commits later, after this block — and a stop it moved was
            # then recorded with no "Why".
            if hasattr(db, "flush"):
                try:
                    db.flush()
                except Exception:  # noqa: BLE001 — the caller's commit reports it
                    log.debug("discord: flush after alert %s failed", getattr(msg, "id", None), exc_info=True)
            return result
    return _w


@_with_alert_reason
def _execute_signal(
    db: Session,
    user: User,
    msg: DiscordMessage,
    background: BackgroundTasks,
    request: Request,
) -> None:
    """Place an approved alert, or record why it couldn't be.

    Runs the SAME validation in paper and live mode — that's the point of paper
    mode. The only difference is the final broker call, so a parser proven in
    paper has been proven against the real checks, not a simplified path.

    Never raises: a refusal or a broker error is recorded on the alert, because
    a failed order must not also fail the request that approved it (in auto mode
    that request is the listener's message intake, and failing it would stall
    the whole feed).
    """
    if discord_execution.already_executed(msg):
        return  # one alert, one order

    if (msg.parsed_signal or {}).get("close_all_matching"):
        _close_all_from_channel(db, user, msg, background, request)
        return

    from app.models.settings import TraderSettings  # noqa: PLC0415 — avoid a cycle

    # The alert's channel decides an ENTRY: its own settings, or the account's
    # while it follows them. An exit switches to the opening channel's below.
    ts_for_sizing = discord_channel_settings.effective(db, user.id, getattr(msg, "source_id", None))
    # Will this entry go at MARKET? Then it is sized and capped against the
    # live price, not the alert's (a market order pays the market).
    try:
        from app.services import market_hours as _mh  # noqa: PLC0415

        goes_at_market = bool(
            ((msg.parsed_signal or {}).get("at_market")
             or discord_channel_settings.entry_order_type(db, getattr(msg, "source_id", None)) == "market")
            and _mh.in_regular_session()
        )
    except Exception:  # noqa: BLE001
        goes_at_market = False
    # ONE way to size: dollars alone, or contracts with at most one cap.
    rules = discord_channel_settings.active_sizing(ts_for_sizing)
    sizing = discord_execution.Sizing(
        multiplier=rules["contracts"] or 1,
        max_per_contract=rules["max_per_contract"],
        max_per_order=rules["max_per_order"],
        mode=rules["mode"],
        dollars=rules["dollars"],
        at_market=goes_at_market,
    )

    signal = msg.parsed_signal or {}

    # "Adding .4" names no contract: it means the position THIS channel is in.
    # Fill the contract from the channel's own latest still-held buy; with
    # nothing held from this channel there is nothing to add to. "Added to TSLA"
    # names the ticker only: the same, among this channel's TSLA contracts.
    if signal.get("add_to_latest") and not (signal.get("strike") and signal.get("option_type")):
        named = signal.get("symbol") or None
        try:
            latest = discord_execution.latest_channel_contract(db, user, msg.source_id, symbol=named)
        except Exception as exc:  # noqa: BLE001
            discord_execution.mark_failed(msg, f"Couldn't find this channel's position: {exc}")
            log.exception("discord: add-to-latest lookup failed for alert %s", msg.id)
            return
        if latest is None:
            # Nothing held — stopped out of it. The author is adding, so they
            # are still in: re-enter the channel's latest contract as a new
            # position rather than skip the alert.
            try:
                latest = discord_execution.latest_channel_contract(
                    db, user, msg.source_id, held_only=False, symbol=named)
            except Exception as exc:  # noqa: BLE001
                discord_execution.mark_failed(msg, f"Couldn't find this channel's position: {exc}")
                log.exception("discord: add-to-latest lookup failed for alert %s", msg.id)
                return
        if latest is None and named:
            # Nothing from this channel in that ticker: resolve() fills the
            # contract from the option position held, as for any add.
            pass
        elif latest is None:
            discord_execution.mark_failed(
                msg, "An add with no contract, and this channel has no recent contract to re-enter."
            )
            return
        else:
            signal = {**signal, **latest}

    # The same channel posting the same entry again (a corrected price, a
    # re-post) is a correction of the trade it already placed, not a second one.
    try:
        from app.services import discord_repost  # noqa: PLC0415

        prior = discord_repost.find_recent_entry(db, msg, signal)
        if prior is not None:
            msg.status = DiscordMessageStatus.PARSED
            msg.status_reason = discord_repost.absorb(db, msg, *prior, signal)[:480]
            log.info("discord: alert %s absorbed into %s — %s", msg.id, prior[1].id, msg.status_reason)
            return
    except Exception:  # noqa: BLE001
        # Never let this check stop an alert: it is a guard, not the trade.
        log.exception("discord: re-post check failed for alert %s", msg.id)

    # The same channel switching to a DIFFERENT contract on this ticker within
    # the window: cancel the first entry if it never filled, flag it if it did,
    # then trade this one as usual. Never sells.
    try:
        from app.services import discord_repost  # noqa: PLC0415

        switched = discord_repost.find_switched_entry(db, msg, signal)
        if switched is not None:
            note = discord_repost.supersede(db, msg, *switched, signal, background=background)
            log.info("discord: alert %s %s", msg.id, note)
    except Exception:  # noqa: BLE001
        log.exception("discord: contract-switch check failed for alert %s", msg.id)

    # An entry that never filled — even after the +10% retry — is a bid for a
    # position the trader is already exiting. Left resting it can still fill
    # later, buying into a move whose exit signal has been given, with no rung
    # of the ladder left to protect it. Cancel it, and cascade to subscribers'
    # mirrors, which are resting on the same stale bid.
    #
    # BEFORE resolve(), not after: an exit alert usually names only part of the
    # contract and resolve() completes it from the open position, so with an
    # unfilled entry there IS no position, resolve() refuses, and anything after
    # it never runs. That is exactly the case this exists for.
    #
    # Only ever fires on the FIRST exit alert for the contract: it cancels what
    # it finds, so by the second there is nothing left.
    try:
        for _oid in discord_execution.cancel_stale_entries_for_signal(db, user, signal):
            from app.api.trades import _run_cancel_fanout_in_background  # noqa: PLC0415
            if background is not None:
                background.add_task(_run_cancel_fanout_in_background, _oid)
            else:
                _run_cancel_fanout_in_background(_oid)
    except Exception:  # noqa: BLE001
        # Never let this stop the exit itself — getting OUT is the point of the
        # alert, and a stray resting entry is the lesser problem.
        log.exception("discord: stale-entry cancel failed for alert %s", msg.id)

    try:
        resolved = discord_execution.resolve(db, user, signal, sizing)
    except discord_execution.ExecutionRefused as exc:
        discord_execution.mark_failed(msg, str(exc))
        log.info("discord: alert %s not placed — %s", msg.id, exc)
        return
    except Exception as exc:  # noqa: BLE001
        discord_execution.mark_failed(msg, f"Couldn't prepare the order: {exc}")
        log.exception("discord: resolve failed for alert %s", msg.id)
        return

    live = bool(ts_for_sizing and ts_for_sizing.discord_live_trading)
    detail = " · ".join(f"{k}={v}" for k, v in resolved.resolutions.items())

    # An exit alert is a rung on a ladder, not a flatten. The first sells half
    # and stops the rest below entry, the second halves again and lifts that
    # stop to break-even, the third exits what's left. Decided before placing
    # anything, because a rung can legitimately place no order at all.
    p = resolved.payload
    is_trim = False
    trim_guard = None
    # The guard a full exit retired before placing it — reopened if the order
    # never reaches the broker, or the position would be held and unmanaged.
    final_guard = None
    if resolved.is_closing:
        cfg = _trim_config(ts_for_sizing)
        guard = guards.find(
            db, user.id, p.symbol, p.option_strike, p.option_right, p.option_expiry
        )
        # Manual exits: the channel that OPENED the position leaves every exit
        # to the trader, so an exit alert is recorded and nothing is sold. An
        # exit the trader types into the composer (the Self channel) IS them
        # closing by hand, so it goes through.
        exit_ts = (discord_channel_settings.for_guard(db, user.id, guard)
                   if guard is not None else None) or ts_for_sizing
        if discord_channel_settings.exits_manual(exit_ts) and not _is_self_alert(db, msg):
            msg.status = DiscordMessageStatus.PARSED
            msg.status_reason = (
                "Exits are manual for this channel — this exit alert was not acted on. "
                "Close it from Positions."
            )
            log.info("discord: exit alert %s for %s skipped — manual exits", msg.id, p.symbol)
            events.publish(user.id, {
                "type": "discord.trim_skipped", "message_id": str(msg.id),
                "symbol": p.symbol, "rung": 0, "reason": "exits are manual for this channel",
            })
            return
        if guard is None:
            # A position the ladder never saw open — opened by hand, or before
            # this feature. Start it on rung one against the broker's own cost
            # basis rather than refusing: the trader is asking to work out of
            # something they hold, and we have a usable reference for it.
            guard = guards.on_buy(
                db, user.id, p.symbol, p.option_strike, p.option_right,
                p.option_expiry, entry_price=resolved.position_entry_price,
            )
        elif guard.entry_price is None and resolved.position_entry_price is not None:
            guard.entry_price = resolved.position_entry_price

        # An exit runs on the ladder — and test/live — of the channel that
        # OPENED the position, not the one posting the exit: an auto-trim fires
        # through Self, and must still trim a Clint position on Clint's ladder.
        origin = discord_channel_settings.for_guard(db, user.id, guard)
        if origin is not None:
            cfg = _trim_config(origin)
            live = bool(getattr(origin, "discord_live_trading", False))

        # resolve() sized this as a full close, so payload.quantity is the
        # whole position — which is exactly what the ladder measures against.
        held = Decimal(str(p.quantity))
        # Before ANY rung is measured: the ladder keys every level off the entry
        # price, and until the opening order fills that is only the limit we bid.
        guards.sync_entry_price(db, guard)
        if signal.get("flatten"):
            # The channel called a full close. The ladder's profit gates exist
            # to decide how much a TRIM sells; they must not keep a position
            # open that the author has exited (live 2026-09-30: a JPM "Close"
            # at -13% sold nothing because rung 2 wanted +35%). Sell it all.
            plan = guards.TrimPlan(
                rung=guard.sell_count or 0, guard=guard, sell_qty=held,
                exit_style=guards.MARKET, retire=True,
                note=f"close alert — selling all {held}",
            )
        else:
            plan = guards.plan_exit(guard, held, resolved.mark_price, cfg)

        guards.apply_stop(guard, plan)
        if plan.retire:
            guards.retire(db, guard, f"trim {plan.rung}: {plan.note}"[:120])

        # Nothing leaves on this rung — a gate that didn't open, or a position
        # that's already gone. Record why and stop here.
        if plan.sell_qty <= 0:
            msg.status = DiscordMessageStatus.PARSED
            msg.status_reason = f"Exit alert {plan.rung} — {plan.note}."
            log.info("discord: exit %s for %s — %s", plan.rung, p.symbol, plan.note)
            events.publish(user.id, {
                "type": "discord.trim_skipped", "message_id": str(msg.id),
                "symbol": p.symbol, "rung": plan.rung, "reason": plan.note,
            })
            return

        # An expensive contract rides a trailing give-back instead of going out
        # now. Nothing is placed today; the poller exits it when it gives back.
        if plan.exit_style == guards.TRAIL:
            guards.arm_trail(guard, plan.sell_qty, plan.trail_amount, resolved.mark_price)
            msg.status = DiscordMessageStatus.PARSED
            msg.status_reason = (
                f"Exit alert {plan.rung} — {plan.sell_qty} of {held} {p.symbol} "
                f"armed to exit on a ${_plain(plan.trail_amount)} trailing give-back."
                + (f" Stop on the rest moved to {_plain(plan.new_stop_price)}."
                   if plan.new_stop_price is not None else "")
            )
            log.info("discord: exit %s armed trailing exit of %s %s",
                     plan.rung, plan.sell_qty, p.symbol)
            events.publish(user.id, {
                "type": "discord.trail_armed", "message_id": str(msg.id),
                "symbol": p.symbol, "quantity": str(plan.sell_qty),
                "trail_amount": str(plan.trail_amount),
            })
            return

        # A resting stop RESERVES the contracts it covers, so the broker would
        # reject this exit for insufficient quantity. Release it first; the
        # poller re-places a correctly sized stop on whatever is left.
        if guard.stop_order_id:
            from app.services import discord_stop_orders  # noqa: PLC0415

            discord_stop_orders.release(
                db, guard, _cancel_stop_order(db, user)
            )
        # A resting take-profit (and its linked stop) reserves contracts just
        # the same. The poller puts back whatever should rest afterwards.
        if guard.tp_order_id is not None or guard.tp_stop_order_id is not None:
            from app.services import discord_take_profit  # noqa: PLC0415

            discord_take_profit.release(db, guard, _cancel_stop_order(db, user))
            guard.tp_qty = None

        # Market exit of this rung's slice.
        p.quantity = plan.sell_qty
        is_trim = not plan.retire
        trim_guard = guard if is_trim else None
        final_guard = guard if plan.retire else None
        detail += (" · " if detail else "") + f"trim {plan.rung}: {plan.note}"

    # Market entries, where the channel asks for them. The alert's price stays
    # the reference: the dollar caps were checked against it in resolve(), and
    # it is the ladder's provisional entry until the fill replaces it.
    entry_ref_price = p.limit_price
    # Asked for by the alert itself ("... @Market" in the composer), or by the
    # channel's entry setting.
    asked_market = bool((msg.parsed_signal or {}).get("at_market"))
    if (not resolved.is_closing
            and (asked_market
                 or discord_channel_settings.entry_order_type(db, getattr(msg, "source_id", None)) == "market")):
        from app.services import market_hours  # noqa: PLC0415

        far = _price_far_from_market(p)
        if far is not None:
            # The alert's price is nowhere near this instrument's: it was read
            # as the wrong thing (Mark's "Added to TSLA, New avg @0.90" became a
            # TSLA STOCK buy, and at market it filled as shares). Keep the
            # limit — it will not fill — rather than buy at whatever the market is.
            detail += (" · " if detail else "") + (
                f"kept as a limit — the alert's {p.limit_price} is far from the market {far}")
        elif market_hours.in_regular_session():
            resolved.payload = p = p.model_copy(
                update={"order_type": OrderType.MARKET, "limit_price": None}
            )
            detail += (" · " if detail else "") + (
                "at market (@Market)" if asked_market else "at market (channel setting)")
        else:
            # Market orders don't trade outside the regular session; a market
            # entry there would just sit until the open. Keep the alert's limit.
            detail += (" · " if detail else "") + "limit — market entries need the regular session"

    if not live:
        # Paper: everything above ran for real; only the broker call is skipped.
        discord_execution.mark_failed(
            msg,
            "Paper mode — not sent to the broker. Would have placed: "
            f"{resolved.payload.side.value.upper()} {resolved.payload.quantity} "
            f"{resolved.payload.symbol} @ {resolved.payload.limit_price or 'market'}"
            + (f" ({detail})" if detail else ""),
        )
        msg.status = DiscordMessageStatus.PARSED   # not a failure — nothing was attempted
        log.info("discord: alert %s validated in paper mode", msg.id)
        return

    from app.api.trades import _place_trader_order  # noqa: PLC0415 — avoid a cycle

    try:
        order = _place_trader_order(
            db, user, resolved.payload, resolved.broker_account_id,
            background, request,
            # Closes go through the close path so the order is marked is_closing
            # — without it an option SELL becomes SELL_TO_OPEN and is rejected,
            # or opens a naked short.
            resolve_wash_trade=resolved.is_closing,
            # A trim closes part of the position and keeps the rest, so the
            # subscriber fanout must not treat it as the trader leaving.
            partial_close=is_trim,
            # Discord subscribers trade the alert themselves, on their own
            # settings (services/discord_subscribers.py) — independent of this
            # order, so it is not copied to them.
            skip_fanout=True,
        )
    except HTTPException as exc:
        if trim_guard is not None:
            # The slice was never sold, so don't spend the trim step on it.
            guards.rollback_exit(trim_guard)
        _reopen(final_guard)
        discord_execution.mark_failed(msg, f"Broker rejected the order: {exc.detail}")
        log.warning("discord: placement failed for alert %s — %s", msg.id, exc.detail)
        return
    except Exception as exc:  # noqa: BLE001
        if trim_guard is not None:
            guards.rollback_exit(trim_guard)
        _reopen(final_guard)
        discord_execution.mark_failed(msg, f"Order placement failed: {exc}")
        log.exception("discord: placement raised for alert %s", msg.id)
        return

    # A filled entry starts the trail-then-exit sequence for this contract; a
    # completed exit retires it. A TRIM is the exception: part of the position
    # is still open and still protected, so its guard stays live — retiring it
    # would drop the trailing stop and restart the count from zero.
    if resolved.is_closing:
        # A close never OPENS a guard. It used to fall through to on_buy when it
        # was a trim (harmless while on_buy ignored an existing guard), but that
        # now hands the guard the SELL order's id as its entry order -- so the
        # entry price would later be adopted from the exit's fill.
        if not is_trim:
            existing = guards.find(
                db, user.id, p.symbol, p.option_strike, p.option_right, p.option_expiry
            )
            if existing is not None:
                guards.retire(db, existing, "closed by exit alert")
    else:
        opened = guards.on_buy(
            db, user.id, p.symbol, p.option_strike, p.option_right, p.option_expiry,
            # Provisional: the limit we bid is the only reference that exists at
            # placement. The real fill replaces it via sync_entry_price once the
            # order fills -- which matters because the +10% reprice can fill
            # ABOVE this limit, and then it is a price we never paid.
            entry_price=entry_ref_price,
            entry_order_id=order.id,
        )
        # An AVERAGING-DOWN add deliberately lowers the cost basis, so the
        # ladder has to follow it. on_buy holds the reference fixed for ordinary
        # adds on purpose (averaging UP must not raise its own stop); this is
        # the opposite case and it is stated explicitly rather than inferred.
        #
        # Normally the order was sized FROM the position, so what we placed is
        # also what was held — but not always (a light position adds its opening
        # size; a dollar cap can cut the add), so the held quantity is carried.
        # …only when there WAS a position. An add into nothing re-entered as a
        # new position, and its ladder starts from this order like any entry.
        if signal.get("double_up") and (getattr(resolved, "held_quantity", None) or 0) > 0:
            before = opened.entry_price
            guards.average_in(
                db, opened,
                held_qty=getattr(resolved, "held_quantity", None) or p.quantity,
                added_qty=p.quantity,
                added_price=entry_ref_price,
            )
            # …and the ladder's stop with it (a hand-set or trailing stop stays).
            try:
                origin_ts = discord_channel_settings.for_guard(db, user.id, opened) or ts_for_sizing
                if origin_ts is not None:
                    guards.reprice_ladder_stop(opened, before, opened.entry_price, origin_ts)
            except Exception:  # noqa: BLE001
                log.exception("discord: could not move the ladder stop after averaging %s", opened.symbol)

    # A trim leaves a REMAINDER, and that remainder is unprotected until the
    # stop reconciler next runs — up to the account's whole poll interval (10s
    # on Alpaca; 4-13s measured live). The interval exists to ration position
    # reads, not to delay a known event, and a filled trim is a known event.
    #
    # Only once the sell is actually FILLED. Reconcile sizes the stop from the
    # quantity the BROKER reports, so poking it while the sell is still
    # settling would size the stop to the whole position and block that very
    # sell — the reservation problem the release-then-replace order exists for.
    # Unfilled simply falls through to the normal tick, exactly as today.
    from app.models.order import OrderStatus  # noqa: PLC0415 — local elsewhere too

    if is_trim and order.status is OrderStatus.FILLED:
        try:
            from app.services.pnl_poller import poll_now  # noqa: PLC0415

            poll_now(resolved.broker_account_id)
        except Exception:  # noqa: BLE001
            # The stop still gets placed on the next tick; this only ever
            # makes it sooner, so a failure here must not fail the trim.
            log.warning("discord: could not expedite the stop for %s", p.symbol,
                        exc_info=True)

    # The trader's fill card (their own Discord webhook) is posted by the fanout
    # for an order already FILLED at placement — and this order skips the
    # fanout. Later fills are covered by the listeners' fill hooks.
    if getattr(user, "role", None) == UserRole.TRADER and order.status is OrderStatus.FILLED:
        try:
            from app.services import discord_alerts  # noqa: PLC0415

            discord_alerts.emit_trader_fill_alert(order.id)
        except Exception:  # noqa: BLE001
            log.exception("discord: fill card failed for order %s", order.id)

    discord_execution.mark_executed(msg, order.id)
    log.info("discord: alert %s placed as order %s%s", msg.id, order.id,
             f" ({detail})" if detail else "")
    events.publish(
        user.id,
        {"type": "discord.order_placed", "message_id": str(msg.id), "order_id": str(order.id)},
    )


def _is_self_alert(db: Session, msg: DiscordMessage) -> bool:
    """Did the trader type this alert themselves? Either it sits on the Self
    channel, or it was typed into the composer AS another channel — the
    composer signs every alert with the trader's own id as its author."""
    author = getattr(msg, "author_id", None)
    if author and str(author) == str(getattr(msg, "user_id", None)):
        return True
    source_id = getattr(msg, "source_id", None)
    if source_id is None:
        return False
    src = db.get(DiscordAlertSource, source_id)
    return src is not None and src.channel_id == _SELF_CHANNEL_ID


def _channel_exits_manual(db: Session, user: User, msg: DiscordMessage) -> bool:
    """Does the alert's own channel leave exits to the trader? Never true for
    the Self channel: an exit typed there is the trader closing by hand."""
    ts = discord_channel_settings.effective(db, user.id, getattr(msg, "source_id", None))
    return discord_channel_settings.exits_manual(ts) and not _is_self_alert(db, msg)


def _close_all_from_channel(
    db: Session, user: User, msg: DiscordMessage, background: BackgroundTasks,
    request: Request,
) -> None:
    """"Stopped out of rest of SPY calls": close, in full and at market, every
    matching contract this channel opened that is still held.

    Each contract goes through the ordinary full-close path (_execute_signal
    with flatten), so ladders retire, paper mode stays paper and subscribers
    behave exactly as for any close. One alert normally means one order; here
    the alert keeps the first order and a line per contract.
    """
    sig = dict(msg.parsed_signal or {})
    if sig.get("latest_contract") and not sig.get("symbol"):
        # "Cutting @here" names nothing: it means the position this channel is
        # in — the contract it most recently bought that is still held.
        try:
            latest = discord_execution.latest_channel_contract(db, user, msg.source_id)
        except Exception as exc:  # noqa: BLE001
            discord_execution.mark_failed(msg, f"Couldn't find this channel's position: {exc}")
            log.exception("discord: latest-contract lookup failed for alert %s", msg.id)
            return
        if latest is None:
            discord_execution.mark_failed(
                msg, "A close-out with no ticker, and nothing from this channel is held.")
            return
        sig = {**sig, "symbol": latest["symbol"], "option_type": latest.get("option_type"),
               "strike": latest.get("strike")}
    what = f"{sig.get('symbol')} {(sig.get('option_type') or '').lower() + 's' if sig.get('option_type') else 'options'}"
    # Everything this closes was opened by this channel, so its settings decide:
    # with Manual exits a stop-out is recorded and nothing is sold.
    if _channel_exits_manual(db, user, msg):
        msg.status = DiscordMessageStatus.PARSED
        msg.status_reason = (
            f"Exits are manual for this channel — this stop-out of {what} was not acted on. "
            "Close it from Positions."
        )
        return
    try:
        contracts = discord_execution.channel_held_contracts(
            db, user, msg.source_id, sig.get("symbol") or "",
            option_type=sig.get("option_type"),
            strike=discord_execution._dec(sig.get("strike")),
        )
    except Exception as exc:  # noqa: BLE001
        discord_execution.mark_failed(msg, f"Couldn't look up this channel's positions: {exc}")
        log.exception("discord: stop-out lookup failed for alert %s", msg.id)
        return
    if not contracts:
        discord_execution.mark_failed(msg, f"Stopped out — you hold no {what} opened from this channel.")
        return

    original = msg.parsed_signal
    lines: list[str] = []
    first_order = None
    try:
        for c in contracts:
            msg.parsed_signal = {**sig, **c, "close_all_matching": False,
                                 "flatten": True, "position_closed": True,
                                 "contract_unspecified": False, "expiry_unspecified": False}
            msg.order_id = None
            msg.status = DiscordMessageStatus.PARSED
            msg.status_reason = None
            _execute_signal(db, user, msg, background, request)
            label = f"{c['symbol']} {c['strike']}{c['option_type'][0].upper()} {c['expiration']}"
            if msg.order_id is not None:
                first_order = first_order or msg.order_id
                lines.append(f"{label}: closed")
            else:
                lines.append(f"{label}: {msg.status_reason or 'not closed'}")
    finally:
        msg.parsed_signal = original
    msg.order_id = first_order
    msg.status = (DiscordMessageStatus.ORDER_CREATED if first_order is not None
                  else DiscordMessageStatus.ORDER_FAILED)
    msg.status_reason = ("Stopped out — " + "; ".join(lines))[:480]


@router.post("/signals/{message_id}/decision", response_model=DiscordDecisionOut)
def decide_signal(
    message_id: uuid.UUID,
    background: BackgroundTasks,
    request: Request,
    accept: bool = Query(..., description="true = approve for execution, false = reject"),
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> DiscordDecisionOut:
    """Accept or reject one parsed alert (manual mode).

    Approving is the hand-off point broker execution will plug into later —
    nothing is sent to a broker today, the alert is simply marked ready.

    Only a PARSED alert can be decided on. Chatter and unreadable messages have
    no signal behind them, and approving one would mean approving nothing.
    """
    msg = db.get(DiscordMessage, message_id)
    if msg is None or msg.user_id != user.id:
        raise HTTPException(404, "not_found")
    if msg.status is not DiscordMessageStatus.PARSED:
        raise HTTPException(409, "alert_has_no_signal")
    # Terminal by design: re-deciding an executed alert would misrepresent what
    # was actually approved at the time it mattered.
    if msg.decision is SignalDecision.REJECTED and accept:
        raise HTTPException(409, "already_rejected")

    msg.decision = SignalDecision.APPROVED if accept else SignalDecision.REJECTED
    msg.decision_mode = "manual"
    msg.decided_at = datetime.now(timezone.utc)

    # Approving IS the instruction to trade — placing it here, in the same
    # request, means the trader gets the outcome immediately rather than
    # discovering it later in a list.
    if accept:
        _execute_signal(db, user, msg, background, request)
    db.commit()

    log.info(
        "discord: alert %s %s by user=%s",
        message_id, "approved" if accept else "rejected", user.id,
    )
    events.publish(
        user.id,
        {"type": "discord.signal_decided", "message_id": str(msg.id),
         "decision": msg.decision.value},
    )
    return DiscordDecisionOut(
        id=msg.id, decision=msg.decision.value, decided_at=msg.decided_at
    )


@router.get("/{source_id}/messages", response_model=Page[DiscordMessageOut])
def list_messages(
    source_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[DiscordMessageOut]:
    """Messages observed on this source, newest first.

    The audit trail behind every signal: what arrived, what the pipeline decided
    about it, and which order (if any) it produced. Ownership is enforced via
    ``_get_owned`` — a trader can only read their own channels' messages.

    Ordered by created_at (when WE ingested it) rather than posted_at, so a
    backlog replay after a restart doesn't scatter newly-arrived rows into the
    middle of the list.
    """
    src = _get_owned(db, user, source_id)

    where = [DiscordMessage.source_id == src.id]
    if status_filter:
        # Coerce to the enum so an unknown value is a clean 400 rather than a
        # database-level cast error.
        try:
            where.append(DiscordMessage.status == DiscordMessageStatus(status_filter))
        except ValueError:
            raise HTTPException(400, f"invalid_status: {status_filter}")

    total = db.execute(
        select(func.count()).select_from(DiscordMessage).where(*where)
    ).scalar_one()
    rows = list(
        db.execute(
            select(DiscordMessage)
            .where(*where)
            .order_by(DiscordMessage.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return Page[DiscordMessageOut](
        items=[DiscordMessageOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(
    source_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
):
    # No `-> None` return annotation on purpose: this module uses
    # `from __future__ import annotations`, which would make FastAPI resolve the
    # annotation into a response model and trip the 204 "no body" assertion.
    src = _get_owned(db, user, source_id)
    db.delete(src)
    db.commit()


# ── Internal routes (discord-listener container only) ────────────────────────

@router.get(
    "/internal/assignments",
    response_model=list[DiscordAssignmentOut],
    dependencies=[Depends(require_listener_token)],
)
def listener_assignments(db: Session = Depends(get_db)) -> list[DiscordAssignmentOut]:
    """Every channel the listener should currently have open.

    The listener polls this and reconciles: open what's new, close what's gone.
    Same self-healing shape as ``listeners.run_reconciler`` for broker listeners
    — a missed control message can only delay a watcher, never strand one.

    This is the one response that carries decrypted Discord sessions. Only
    enabled sources with a stored session appear; a source whose session fails
    to decrypt (encryption key rotated) is skipped and flipped to 'needs_login'
    so the trader is told to re-authorise instead of the listener retrying a
    credential that can never work.
    """
    rows = list(
        db.execute(
            select(DiscordAlertSource)
            .join(DiscordAccount, DiscordAccount.id == DiscordAlertSource.account_id)
            .where(
                DiscordAlertSource.is_enabled.is_(True),
                DiscordAccount.encrypted_session.is_not(None),
            )
        ).scalars()
    )

    out: list[DiscordAssignmentOut] = []
    dirty = False
    for src in rows:
        # Outside its active window a source is simply withheld; the listener's
        # reconcile then closes the watcher, exactly as if it had been disabled.
        # This is the ONLY place the schedule is enforced — there is no scheduler
        # and no listener-side clock to drift.
        if not discord_schedule.in_window(
            mode=src.schedule_mode,
            start=src.schedule_start,
            end=src.schedule_end,
            timezone=src.schedule_timezone,
            days=list(src.schedule_days or []),
        ):
            if src.status not in ("off_schedule", "needs_login"):
                src.status = "off_schedule"
                src.last_error = None
                dirty = True
            continue

        # Back inside the window — clear the off-schedule marker so the card
        # doesn't sit on a stale label until the watcher reports in.
        if src.status == "off_schedule":
            src.status = "connecting"
            dirty = True

        acct = src.account
        try:
            state = decrypt_session(acct.encrypted_session or "")
        except (ValueError, TypeError):
            log.warning(
                "discord: undecryptable session on account=%s — marking needs_login",
                acct.id if acct else None,
            )
            if acct is not None:
                acct.encrypted_session = None
                acct.session_captured_at = None
                acct.status = "needs_login"
                acct.last_error = "Stored Discord session could not be read. Please sign in again."
            src.status = "needs_login"
            dirty = True
            continue
        out.append(
            DiscordAssignmentOut(
                source_id=src.id,
                user_id=src.user_id,
                guild_id=src.guild_id,
                channel_id=src.channel_id,
                label=src.label,
                last_seen_message_id=src.last_seen_message_id,
                storage_state=state,
            )
        )
    if dirty:
        db.commit()
    return out


def _handle_edits(db: Session, user: User | None, edited, background, request) -> None:
    """An EDITED alert is a correction to the trade it already placed, never a
    new one — so it bypasses execution and repoints the resting order instead.
    An edit that switches the CONTRACT cancels the unfilled entry and trades the
    edited one (services/discord_repost.switch_on_edit).

    Handled per message: one failure must not stop the rest of the batch, and a
    bad edit must never fail the listener's POST. The outcome is recorded on the
    row, not just logged — "the edit did nothing" and "the edit was never seen"
    look identical in an order history and mean completely different things."""
    from app.services import discord_repost  # noqa: PLC0415

    for msg in edited:
        try:
            outcome = discord_edit.apply_price_edit(db, msg)
            if outcome == discord_edit.DIFFERENT_CONTRACT:
                outcome, trade = discord_repost.switch_on_edit(db, msg, background=background)
                if trade and user is not None:
                    _execute_signal(db, user, msg, background, request)
                    if msg.status is DiscordMessageStatus.ORDER_FAILED:
                        outcome += f"; the new contract was not placed — {msg.status_reason}"
            msg.status_reason = f"Edited alert: {outcome}"[:480]
            log.info("discord: edited alert %s — %s", msg.discord_message_id, outcome)
        except Exception as exc:  # noqa: BLE001
            msg.status_reason = f"Edited alert: handling failed — {exc}"[:480]
            log.exception("discord: edit handling failed for %s", msg.discord_message_id)


def _handle_deletions(db: Session, deleted, background) -> None:
    """The author deleted an alert: withdraw the entry it placed if that never
    filled; flag it if it did. Never sells. Per message, never raises."""
    from app.services import discord_repost  # noqa: PLC0415

    for msg in deleted:
        try:
            outcome = discord_repost.apply_delete(db, msg, background=background)
        except Exception as exc:  # noqa: BLE001
            outcome = f"handling failed — {exc}"
            log.exception("discord: delete handling failed for %s", msg.discord_message_id)
        msg.status_reason = f"{discord_repost.DELETED_PREFIX} — {outcome}"[:480]
        log.info("discord: deleted alert %s — %s", msg.discord_message_id, outcome)


@router.post(
    "/internal/messages",
    response_model=DiscordIngestOut,
    dependencies=[Depends(require_listener_token)],
)
def listener_messages(
    payload: DiscordMessageBatchIn,
    background: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
) -> DiscordIngestOut:
    """Accept a batch of observed messages, de-duplicate, and queue them.

    Returns per-batch counts so the listener can log what actually landed.
    Duplicates are an expected, non-error outcome — a reconnect or re-render
    re-observes messages we already have (PHASE 7).
    """
    src = db.get(DiscordAlertSource, payload.source_id)
    if src is None:
        raise HTTPException(404, "source_not_found")

    max_batch = get_settings().discord_ingest_max_batch
    if len(payload.messages) > max_batch:
        raise HTTPException(413, f"batch_too_large: max {max_batch}")

    # Cross-check the channel the listener claims against the one we assigned.
    # The listener is a separate process reconciling its own page state; a
    # mismatch means it drifted (channel switched under it) and its messages
    # would be attributed to the wrong source.
    wrong = [m.message_id for m in payload.messages if m.channel_id != src.channel_id]
    if wrong:
        log.warning(
            "discord: dropping %d message(s) for source=%s — channel mismatch",
            len(wrong), src.id,
        )

    batch = [m.model_dump() for m in payload.messages if m.channel_id == src.channel_id]
    # Subscribers trade the ALERT on their own settings, not the trader's order
    # — started first, on threads of their own, so nobody waits on the trader's
    # broker. See services/discord_subscribers.py.
    try:
        discord_subscribers.relay_batch(db, src, batch)
    except Exception:  # noqa: BLE001 — never stall the trader's own feed
        log.exception("discord: subscriber relay failed for source %s", src.id)
    auto = _auto_approve(db, src.user_id, src.id)
    report = discord_ingest.ingest_batch(db, src, batch, auto_approve=auto)

    # Auto mode: a parse IS the approval, so place it now. Each alert is handled
    # independently — one refusal must not stop the others in the batch.
    if auto and report.accepted:
        owner = db.get(User, src.user_id)
        if owner is not None:
            from app.services import discord_freshness  # noqa: PLC0415

            for msg in report.stored:
                # A late entry — the backlog a reconnecting listener replays —
                # waits for approval instead of buying at today's price.
                if discord_freshness.hold_if_stale(msg):
                    log.info("discord: alert %s held — arrived late", msg.discord_message_id)
                    continue
                if msg.decision is SignalDecision.APPROVED:
                    _execute_signal(db, owner, msg, background, request)

    owner = db.get(User, src.user_id)
    _handle_edits(db, owner, report.edited, background, request)
    _handle_deletions(db, report.deleted, background)

    for mid in wrong:
        report.rejected.append({"message_id": mid, "reason": "channel_mismatch"})
    db.commit()
    return DiscordIngestOut(**report.as_dict())


@router.post(
    "/internal/status",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_listener_token)],
)
def listener_status(
    payload: DiscordListenerStatusIn,
    db: Session = Depends(get_db),
):
    """Connection state / heartbeat for one source.

    Heartbeats matter because a quiet channel is indistinguishable from a dead
    watcher by ``last_message_at`` alone — an alert channel can legitimately go
    hours without a post.
    """
    src = db.get(DiscordAlertSource, payload.source_id)
    if src is None:
        raise HTTPException(404, "source_not_found")
    discord_ingest.record_status(
        src,
        payload.status,
        error=payload.error,
        channel_name=payload.channel_name,
        guild_name=payload.guild_name,
        baseline_message_id=payload.baseline_message_id,
    )
    db.commit()


# ── QR login: listener side ─────────────────────────────────────────────────

@router.get(
    "/internal/login-requests",
    response_model=list[DiscordLoginRequestOut],
    dependencies=[Depends(require_listener_token)],
)
def listener_login_requests() -> list[DiscordLoginRequestOut]:
    """Login attempts awaiting the listener.

    Polled on the same sweep as channel assignments — the listener has no inbound
    port, so every instruction reaches it by polling.
    """
    return [
        DiscordLoginRequestOut(
            session_id=s["session_id"], source_id=s["source_id"], status=s["status"]
        )
        for s in discord_login.pending()
    ]


@router.post(
    "/internal/login/{session_id}/qr",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_listener_token)],
)
def listener_login_qr(session_id: uuid.UUID, payload: DiscordLoginQrIn):
    """A freshly captured QR frame. Called repeatedly as Discord rotates it, so
    the trader never sees a stale code."""
    if discord_login.set_qr(session_id, payload.qr_png) is None:
        raise HTTPException(404, "login_session_not_found")


@router.post(
    "/internal/login/{session_id}/status",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_listener_token)],
)
def listener_login_status(session_id: uuid.UUID, payload: DiscordLoginStatusIn):
    if payload.status == "failed":
        result = discord_login.finish(session_id, error=payload.error or "Login failed.")
    else:
        result = discord_login.set_status(session_id, payload.status, error=payload.error)
    if result is None:
        raise HTTPException(404, "login_session_not_found")


@router.post(
    "/internal/login/{session_id}/complete",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_listener_token)],
)
def listener_login_complete(
    session_id: uuid.UUID,
    payload: DiscordLoginCompleteIn,
    db: Session = Depends(get_db),
):
    """Store the captured Discord session against its source.

    Same validation and encryption as a manual upload — the capture route
    differs, the handling of the credential does not.
    """
    session = discord_login.get(session_id)
    if session is None:
        raise HTTPException(404, "login_session_not_found")

    src = db.get(DiscordAlertSource, uuid.UUID(session["source_id"]))
    if src is None:
        discord_login.finish(session_id, error="The source was removed.")
        raise HTTPException(404, "source_not_found")

    try:
        state = validate_storage_state(payload.storage_state)
    except DiscordSessionError as exc:
        discord_login.finish(session_id, error=str(exc))
        raise HTTPException(400, f"invalid_session: {exc}")

    if _store_session(db, src, state) is None:
        discord_login.finish(session_id, error="This channel has no Discord account.")
        raise HTTPException(409, "source_has_no_account")
    db.commit()

    discord_login.finish(session_id)
    log.info("discord: QR login completed for source=%s", src.id)
    events.publish(
        src.user_id,
        {"type": "discord.login_complete", "source_id": str(src.id)},
    )


# ── Desktop Connector: pairing endpoints ────────────────────────────────────
#
# These are reached by the Connector app, which has no Kopyya login. They are
# authenticated by the pairing code itself (single-use, short-lived, and shown
# only to the source's owner) plus the upload token handed back on claim.
# Deliberately NOT behind require_listener_token: the Connector runs on a
# trader's machine and must never hold the listener's shared secret.

@router.post("/pair/claim", response_model=DiscordPairClaimOut)
def claim_pairing(payload: DiscordPairClaimIn, db: Session = Depends(get_db)) -> DiscordPairClaimOut:
    """Redeem a pairing code. Single use — a second attempt is refused."""
    session = discord_pairing.claim(payload.code)
    if session is None:
        # Deliberately one message for unknown / expired / already-claimed: the
        # difference is only useful to someone guessing codes.
        raise HTTPException(404, "invalid_or_expired_code")

    src = db.get(DiscordAlertSource, uuid.UUID(session["source_id"]))
    if src is None:
        discord_pairing.finish(session["code"], error="The source was removed.")
        raise HTTPException(404, "source_not_found")

    return DiscordPairClaimOut(
        source_id=src.id, label=src.label, upload_token=session["upload_token"]
    )


@router.post("/pair/complete", status_code=status.HTTP_204_NO_CONTENT)
def complete_pairing(payload: DiscordPairCompleteIn, db: Session = Depends(get_db)):
    """Store the session the Connector captured on the trader's machine.

    Same validation and Fernet encryption as every other capture route — the
    capture method differs, the handling of the credential does not.
    """
    session = discord_pairing.authorise(payload.code, payload.upload_token)
    if session is None:
        raise HTTPException(403, "invalid_pairing")

    src = db.get(DiscordAlertSource, uuid.UUID(session["source_id"]))
    if src is None:
        discord_pairing.finish(session["code"], error="The source was removed.")
        raise HTTPException(404, "source_not_found")

    try:
        state = validate_storage_state(payload.storage_state)
    except DiscordSessionError as exc:
        discord_pairing.finish(session["code"], error=str(exc))
        raise HTTPException(400, f"invalid_session: {exc}")

    # Store on the ACCOUNT, then bring every channel it reads online at once —
    # that is what makes one sign-in cover all of them.
    acct = _store_session(db, src, state)
    if acct is None:
        discord_pairing.finish(session["code"], error="This channel has no Discord account.")
        raise HTTPException(409, "source_has_no_account")
    db.commit()

    discord_pairing.finish(session["code"])
    log.info(
        "discord: connector paired account=%s (%d channel(s) now live)",
        acct.id, len(acct.sources),
    )
    events.publish(
        src.user_id, {"type": "discord.login_complete", "source_id": str(src.id)}
    )
