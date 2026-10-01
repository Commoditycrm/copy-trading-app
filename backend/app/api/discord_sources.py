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
from app.models.order import Order, OrderSide, OrderStatus
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
    DiscordSelfAlertIn,
    DiscordSelfAlertOut,
)
from app.services import (
    discord_execution,
    discord_position_guard as guards,
    price_override,
    discord_edit,
    discord_ingest,
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
    Simulated Prices dry run both measure against."""
    return guards.TrimConfig(
        trim1=guards.RungConfig(
            _setting(ts, "discord_trim_profit_gate_pct", "20"),
            _setting(ts, "discord_trim_stop_pct", "-25"),
            _setting(ts, "discord_trim_qty_pct", "50"),
        ),
        trim2=guards.RungConfig(
            _setting(ts, "discord_trim2_profit_gate_pct", "0"),
            _setting(ts, "discord_trim2_stop_pct", "0"),
            _setting(ts, "discord_trim2_qty_pct", "50"),
        ),
        trim3=guards.RungConfig(
            _setting(ts, "discord_trim3_profit_gate_pct", "0"),
            _setting(ts, "discord_trim3_stop_pct", "0"),
            _setting(ts, "discord_trim3_qty_pct", "100"),
        ),
        price_threshold=_setting(ts, "discord_trim_price_threshold", "0.90"),
        trail_amount=_setting(ts, "discord_trim_trail_amount", "0.25"),
    )


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


def _auto_approve(db: Session, user_id: uuid.UUID) -> bool:
    """Is this trader's Discord execution mode set to auto?

    One setting for the whole account. Defaults to manual when no settings row
    exists — an alert must never be cleared for execution because a row was
    missing.
    """
    from app.models.settings import TraderSettings  # noqa: PLC0415 — avoid a cycle

    ts = db.get(TraderSettings, user_id)
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
        return [_mirror_out(m, p) for m, p in pairs]
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
    return [_to_out(r) for r in rows]


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
@router.get("/settings", response_model=DiscordSettingsOut)
def get_discord_settings(
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> DiscordSettingsOut:
    """Account-wide handling of inbound Discord alerts."""
    from app.models.settings import TraderSettings  # noqa: PLC0415 — avoid a cycle

    ts = db.get(TraderSettings, user.id)
    return DiscordSettingsOut(
        execution_mode="auto" if _auto_approve(db, user.id) else "manual",
        live_trading=bool(ts and ts.discord_live_trading),
        auto_trim=bool(ts and getattr(ts, "discord_auto_trim", False)),
        quantity_multiplier=(ts.discord_quantity_multiplier if ts else 1) or 1,
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
        reprice_after_seconds=(
            getattr(ts, "discord_reprice_after_seconds", None) or 30 if ts else 30
        ),
        reprice_pct=_plain(_setting(ts, "discord_reprice_pct", "10")),
    )


@router.patch("/settings", response_model=DiscordSettingsOut)
def update_discord_settings(
    payload: DiscordSettingsIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_discord_member),
) -> DiscordSettingsOut:
    """Switch between reviewing every alert and auto-approving parsed ones.

    Applies to alerts arriving from now on. Decisions already recorded keep the
    mode that applied at the time — switching to auto must not retroactively
    approve alerts the trader never saw.
    """
    from app.models.settings import TraderSettings  # noqa: PLC0415 — avoid a cycle

    ts = db.get(TraderSettings, user.id)
    if ts is None:
        ts = TraderSettings(user_id=user.id)
        db.add(ts)
    if payload.execution_mode is not None:
        ts.discord_execution_mode = payload.execution_mode
    if payload.quantity_multiplier is not None:
        ts.discord_quantity_multiplier = payload.quantity_multiplier
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

    if payload.auto_trim is not None:
        ts.discord_auto_trim = payload.auto_trim
        log.info(
            "discord: auto-trim %s for user %s",
            "ENABLED" if payload.auto_trim else "disabled", user.id,
        )

    if payload.live_trading is not None:
        ts.discord_live_trading = payload.live_trading
        log.warning(
            "discord: LIVE TRADING %s for user=%s",
            "ENABLED" if payload.live_trading else "disabled", user.id,
        )
    db.commit()
    return DiscordSettingsOut(
        execution_mode=ts.discord_execution_mode,
        live_trading=bool(ts.discord_live_trading),
        auto_trim=bool(getattr(ts, "discord_auto_trim", False)),
        quantity_multiplier=ts.discord_quantity_multiplier or 1,
        max_per_contract=_plain(ts.discord_max_per_contract),
        max_per_order=_plain(ts.discord_max_per_order),
        trail_percent=_plain(ts.discord_trail_percent) or "20",
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
        reprice_after_seconds=(
            getattr(ts, "discord_reprice_after_seconds", None) or 30 if ts else 30
        ),
        reprice_pct=_plain(_setting(ts, "discord_reprice_pct", "10")),
    )


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


def submit_self_alert_text(
    db: Session,
    user: User,
    content: str,
    *,
    background: BackgroundTasks | None = None,
    request: Request | None = None,
    approve: bool = False,
) -> DiscordMessage | None:
    """Put ``content`` through the Discord pipeline as a Self-channel alert.

    The one place an alert can be injected without Discord — used by the
    composer in Order History and by auto-trim. Returns the stored message so
    the caller can report its verdict, or None if it could not be stored.

    ``approve`` forces execution past the trader's manual/auto gate. Auto-trim
    sets it: the trader turned auto-trim ON, which IS the approval, and an
    automatic trim that sat waiting for a second approval would miss the move
    it was watching for. The composer leaves it False, so a pasted alert behaves
    exactly as if Discord had delivered it.
    """
    src = _self_source(db, user)
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

    auto = approve or _auto_approve(db, user.id)
    report = discord_ingest.ingest_batch(db, src, [raw], auto_approve=auto)
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

    ts_for_sizing = db.get(TraderSettings, user.id)
    sizing = discord_execution.Sizing(
        multiplier=(ts_for_sizing.discord_quantity_multiplier if ts_for_sizing else 1) or 1,
        max_per_contract=(ts_for_sizing.discord_max_per_contract if ts_for_sizing else None),
        max_per_order=(ts_for_sizing.discord_max_per_order if ts_for_sizing else None),
    )

    signal = msg.parsed_signal or {}

    # "Adding .4" names no contract: it means the position THIS channel is in.
    # Fill the contract from the channel's own latest still-held buy; with
    # nothing held from this channel there is nothing to add to.
    if signal.get("add_to_latest") and not signal.get("symbol"):
        try:
            latest = discord_execution.latest_channel_contract(db, user, msg.source_id)
        except Exception as exc:  # noqa: BLE001
            discord_execution.mark_failed(msg, f"Couldn't find this channel's position: {exc}")
            log.exception("discord: add-to-latest lookup failed for alert %s", msg.id)
            return
        if latest is None:
            discord_execution.mark_failed(
                msg, "An add with no contract, and you hold nothing this channel opened to add to."
            )
            return
        signal = {**signal, **latest}

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

        if plan.new_stop_price is not None:
            guard.stop_price = plan.new_stop_price
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

        # Market exit of this rung's slice.
        p.quantity = plan.sell_qty
        is_trim = not plan.retire
        trim_guard = guard if is_trim else None
        final_guard = guard if plan.retire else None
        detail += (" · " if detail else "") + f"trim {plan.rung}: {plan.note}"

    if not live:
        # Paper: everything above ran for real; only the broker call is skipped.
        discord_execution.mark_failed(
            msg,
            "Paper mode — not sent to the broker. Would have placed: "
            f"{resolved.payload.side.value.upper()} {resolved.payload.quantity} "
            f"{resolved.payload.symbol} @ {resolved.payload.limit_price}"
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
            entry_price=p.limit_price,
            entry_order_id=order.id,
        )
        # An AVERAGING-DOWN add deliberately lowers the cost basis, so the
        # ladder has to follow it. on_buy holds the reference fixed for ordinary
        # adds on purpose (averaging UP must not raise its own stop); this is
        # the opposite case and it is stated explicitly rather than inferred.
        #
        # The order was sized FROM the position, so the quantity we just placed
        # is also the quantity that was held.
        if signal.get("double_up"):
            guards.average_in(
                db, opened,
                held_qty=p.quantity, added_qty=p.quantity,
                added_price=p.limit_price,
            )

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
    what = f"{sig.get('symbol')} {(sig.get('option_type') or '').lower() + 's' if sig.get('option_type') else 'options'}"
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
    auto = _auto_approve(db, src.user_id)
    report = discord_ingest.ingest_batch(db, src, batch, auto_approve=auto)

    # Auto mode: a parse IS the approval, so place it now. Each alert is handled
    # independently — one refusal must not stop the others in the batch.
    if auto and report.accepted:
        owner = db.get(User, src.user_id)
        if owner is not None:
            for msg in report.stored:
                if msg.decision is SignalDecision.APPROVED:
                    _execute_signal(db, owner, msg, background, request)

    # An EDITED alert is a correction to the trade it already placed, never a
    # new one — so it deliberately bypasses _execute_signal above and repoints
    # the resting order instead. Handled per message: one failure must not stop
    # the rest of the batch, and a bad edit must never fail the listener's POST.
    for msg in report.edited:
        try:
            outcome = discord_edit.apply_price_edit(db, msg)
            # Recorded on the row, not just logged. "The edit did nothing" and
            # "the edit was never seen" look identical in an order history and
            # mean completely different things — and the log is the one place
            # nobody has when they ask why the price did not move.
            msg.status_reason = f"Edited alert: {outcome}"[:480]
            log.info("discord: edited alert %s — %s", msg.discord_message_id, outcome)
        except Exception as exc:  # noqa: BLE001
            msg.status_reason = f"Edited alert: handling failed — {exc}"[:480]
            log.exception("discord: edit handling failed for %s", msg.discord_message_id)

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
