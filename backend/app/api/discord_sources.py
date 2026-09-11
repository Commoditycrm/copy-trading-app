"""Inbound Discord alert sources — STEP 2: connection + real-time listener.

A trader connects a Discord channel that Kopyaa monitors for trade alerts. This
router manages the connection lifecycle and serves as the boundary the
``discord-listener`` container talks to. It does NOT parse trades, validate
signals, or place orders — those are steps 4-6, and keeping them out of here is
deliberate: a message is only data until the parser has looked at it.

── Ingestion model ──────────────────────────────────────────────────────────────
We monitor Discord Web as the trader's OWN logged-in account, reading only the
channels that account can already legitimately open. The trader signs in
themselves in a real browser (password + MFA never touch Kopyaa) and we store the
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

import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import require_trader
from app.config import get_settings
from app.database import get_db
from app.models.discord_account import DiscordAccount
from app.models.discord_alert_source import DiscordAlertSource
from app.models.discord_message import DiscordMessage, DiscordMessageStatus, SignalDecision
from app.models.user import User
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
)
from app.services import discord_ingest, discord_login, discord_pairing, discord_schedule, events
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


def _require_feature() -> None:
    """503 when the inbound Discord feature is switched off.

    Without this, a trader could connect a source in an environment that has no
    listener container, then watch it sit at 'connecting' forever with no
    explanation. Off is the default.
    """
    if not get_settings().discord_listener_enabled:
        raise HTTPException(503, "discord_listener_disabled")


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
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
) -> list[DiscordSourceOut]:
    rows = db.execute(
        select(DiscordAlertSource)
        .where(DiscordAlertSource.user_id == user.id)
        .order_by(DiscordAlertSource.created_at.desc())
    ).scalars()
    return [_to_out(r) for r in rows]


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


@router.get("/signals/page", response_model=Page[DiscordSignalOut])
def list_signals(
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
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
    items: list[DiscordSignalOut] = []
    for m, src in rows:
        signals = list(m.parsed_signals or ([m.parsed_signal] if m.parsed_signal else []))
        if not signals:
            items.append(_signal_out(m, src, {}, 0))
            continue
        for idx, sig in enumerate(signals):
            items.append(_signal_out(m, src, sig or {}, idx))

    return Page[DiscordSignalOut](
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


def _signal_out(
    m: DiscordMessage, src: DiscordAlertSource, sig: dict, idx: int = 0
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
    )


# NOTE: registered BEFORE "/{source_id}" on purpose — FastAPI matches in
# registration order, and a literal segment must win over the UUID converter.
@router.get("/settings", response_model=DiscordSettingsOut)
def get_discord_settings(
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
) -> DiscordSettingsOut:
    """Account-wide handling of inbound Discord alerts."""
    return DiscordSettingsOut(
        execution_mode="auto" if _auto_approve(db, user.id) else "manual"
    )


@router.patch("/settings", response_model=DiscordSettingsOut)
def update_discord_settings(
    payload: DiscordSettingsIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
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
    ts.discord_execution_mode = payload.execution_mode
    db.commit()
    log.info("discord: execution mode set to %s for user=%s", payload.execution_mode, user.id)
    return DiscordSettingsOut(execution_mode=payload.execution_mode)


@router.patch("/{source_id}", response_model=DiscordSourceOut)
def update_source(
    source_id: uuid.UUID,
    payload: DiscordSourceUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
) -> DiscordSourceOut:
    src = _get_owned(db, user, source_id)
    if payload.label is not None:
        src.label = payload.label.strip()
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

    Kopyaa never performs the login: the trader signs in themselves in a real
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
    approves on their phone — their password and MFA never touch Kopyaa.
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
    """Mint a pairing code for the Kopyaa Connector desktop app.

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


@router.post("/signals/{message_id}/decision", response_model=DiscordDecisionOut)
def decide_signal(
    message_id: uuid.UUID,
    accept: bool = Query(..., description="true = approve for execution, false = reject"),
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
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
    user: User = Depends(require_trader),
    _: None = Depends(_require_feature),
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
    report = discord_ingest.ingest_batch(
        db, src, batch, auto_approve=_auto_approve(db, src.user_id)
    )
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
# These are reached by the Connector app, which has no Kopyaa login. They are
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
