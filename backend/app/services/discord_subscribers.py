"""Discord for subscribers: their own copy of each trader channel.

A subscriber following a Discord trader does not copy the trader's Discord
ORDERS. They receive the trader's channel ALERTS and trade them on their own
settings — sizing, approval, paper/live, exit ladder, auto-trim, AI trimming —
exactly as the trader's own account does. The two are independent: a trader
order rejected, cancelled or sized differently has no bearing on theirs.

How: every trader channel gets a mirror source per follower
(``DiscordAlertSource.parent_source_id``), owned by the subscriber. Alerts the
listener delivers for the trader's channel are ingested into each mirror and run
through the unchanged pipeline (``ingest_batch`` → ``_execute_signal``) as the
subscriber. Because the mirror is an ordinary source that the subscriber owns,
everything keyed on source or user works for them unchanged: manual approvals,
the Order History Discord tab, "Adding .4" (the channel's latest position), the
Channel column, ladder guards, the auto-trim sweep.

The mirror has no Discord account, so the listener never opens it. Its
``is_enabled`` is the subscriber's switch: off skips new ENTRIES from that
channel, while exits for positions already held still go through.

The mirror is the subscriber's: when the trader removes the channel, or the
subscriber follows someone else, it is DETACHED (parent cleared), never
deleted — their alert history and order links stay. It is re-attached if the
same channel comes back.
"""
from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.discord_alert_source import DiscordAlertSource
from app.models.discord_message import DiscordMessageStatus, SignalDecision
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole

log = logging.getLogger(__name__)

SELF_CHANNEL_ID = "self"   # the trader's Self source — plumbing, never relayed

# Trader settings a subscriber's own copy starts from. Everything Discord EXCEPT
# the trader's fill-broadcast webhook, which is the trader's alone.
_NOT_COPIED = {"discord_webhook_url", "discord_alerts_enabled"}

CHANNEL_OFF_REASON = "You turned this channel off — new entries from it are skipped."
COPY_OFF_REASON = "Copy trading is off — new entries are skipped."

# One worker per subscriber relay, so every subscriber's order goes out at the
# same time as the trader's instead of one after another.
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="discord-relay")


# ── who is in ────────────────────────────────────────────────────────────────

def followed_discord_trader(db: Session, user: User) -> User | None:
    """The Discord-enabled trader this subscriber follows, else None."""
    if user.role != UserRole.SUBSCRIBER:
        return None
    ss = db.get(SubscriberSettings, user.id)
    if ss is None or ss.following_trader_id is None:
        return None
    trader = db.get(User, ss.following_trader_id)
    if trader is None or not trader.is_active or not trader.discord_enabled:
        return None
    return trader


def has_discord(db: Session, user: User) -> bool:
    """Does this user get the Discord page — a Discord trader, or a subscriber
    of one?"""
    if user.role == UserRole.TRADER:
        return bool(user.discord_enabled)
    return followed_discord_trader(db, user) is not None


def followers(db: Session, trader_id: uuid.UUID) -> list[User]:
    """Every active subscriber following this trader — copy on or off (with
    copy off they still take exits, like the order fanout's close-only path)."""
    return list(db.execute(
        select(User).join(SubscriberSettings, SubscriberSettings.user_id == User.id).where(
            SubscriberSettings.following_trader_id == trader_id,
            User.role == UserRole.SUBSCRIBER,
            User.is_active.is_(True),
        )
    ).scalars())


# ── mirrors ──────────────────────────────────────────────────────────────────

def trader_channels(db: Session, trader_id: uuid.UUID) -> list[DiscordAlertSource]:
    return list(db.execute(
        select(DiscordAlertSource).where(
            DiscordAlertSource.user_id == trader_id,
            DiscordAlertSource.parent_source_id.is_(None),
            DiscordAlertSource.channel_id != SELF_CHANNEL_ID,
        ).order_by(DiscordAlertSource.created_at.desc())
    ).scalars())


def _copy_identity(mirror: DiscordAlertSource, parent: DiscordAlertSource) -> None:
    mirror.label = parent.label
    mirror.channel_name = parent.channel_name
    mirror.guild_id = parent.guild_id
    mirror.guild_name = parent.guild_name
    # The channel's house style decides how its messages parse — a subscriber
    # must read the same message the same way the trader does.
    mirror.percent_means_exit = parent.percent_means_exit


def ensure_mirror(db: Session, subscriber: User, parent: DiscordAlertSource) -> DiscordAlertSource:
    mirror = db.execute(
        select(DiscordAlertSource).where(
            DiscordAlertSource.user_id == subscriber.id,
            DiscordAlertSource.parent_source_id == parent.id,
        )
    ).scalars().first()
    if mirror is None:
        # A copy of this channel detached earlier (removed and re-added, or a
        # re-follow): re-attach it so the subscriber's history continues. Only
        # detached copies qualify — a subscriber owns no other sources but Self.
        mirror = db.execute(
            select(DiscordAlertSource).where(
                DiscordAlertSource.user_id == subscriber.id,
                DiscordAlertSource.parent_source_id.is_(None),
                DiscordAlertSource.channel_id == parent.channel_id,
                DiscordAlertSource.channel_id != SELF_CHANNEL_ID,
            )
        ).scalars().first()
        if mirror is not None:
            mirror.parent_source_id = parent.id
            mirror.status = "connected"
    if mirror is None:
        mirror = DiscordAlertSource(
            user_id=subscriber.id,
            parent_source_id=parent.id,
            channel_id=parent.channel_id,
            account_id=None,          # never assigned to the listener
            is_enabled=True,
            status="connected",
            label=parent.label,
        )
        _copy_identity(mirror, parent)
        db.add(mirror)
        db.flush()
        log.info("discord: mirrored channel %s for subscriber %s", parent.id, subscriber.id)
    else:
        _copy_identity(mirror, parent)
    return mirror


def sync_mirrors(db: Session, subscriber: User) -> list[tuple[DiscordAlertSource, DiscordAlertSource]]:
    """Bring the subscriber's mirrors in line with the trader they follow now:
    one per trader channel. Copies of channels no longer in the list are
    detached, not deleted (see module docstring). Returns (mirror, parent)
    pairs, newest channel first."""
    trader = followed_discord_trader(db, subscriber)
    parents = trader_channels(db, trader.id) if trader else []
    keep = {p.id for p in parents}
    for stale in db.execute(
        select(DiscordAlertSource).where(
            DiscordAlertSource.user_id == subscriber.id,
            DiscordAlertSource.parent_source_id.is_not(None),
        )
    ).scalars():
        if stale.parent_source_id not in keep:
            stale.parent_source_id = None
            stale.status = "disconnected"
    pairs = [(ensure_mirror(db, subscriber, p), p) for p in parents]
    if trader is not None:
        ensure_settings(db, subscriber, trader)
    db.flush()
    return pairs


def ensure_settings(db: Session, subscriber: User, trader: User) -> TraderSettings:
    """The subscriber's own Discord settings, created on first use as a copy of
    the trader's — so on day one a subscriber trades an alert the way the trader
    does — with approval on auto: following a trader was their approval, and a
    manual default would silently stop the trades they already receive."""
    ts = db.get(TraderSettings, subscriber.id)
    if ts is not None:
        return ts
    src = db.get(TraderSettings, trader.id)
    ts = TraderSettings(user_id=subscriber.id)
    if src is not None:
        for col in TraderSettings.__table__.columns:
            name = col.name
            if name.startswith("discord_") and name not in _NOT_COPIED:
                setattr(ts, name, getattr(src, name))
    ts.discord_execution_mode = "auto"
    db.add(ts)
    db.flush()
    return ts


# ── relay ────────────────────────────────────────────────────────────────────

def _is_entry(msg) -> bool:
    return str((msg.parsed_signal or {}).get("action") or "").upper() == "BUY"


def _entry_block_reason(db: Session, subscriber: User,
                        mirror: DiscordAlertSource) -> str | None:
    """Why this subscriber may not take a new ENTRY right now, if they may not."""
    if not mirror.is_enabled:
        return CHANNEL_OFF_REASON
    # The subscriber's OWN switch (also what their daily loss / profit limits
    # flip). Nothing of the trader's gates this — the trader decides only which
    # channels exist (copy_engine.trades_independently).
    ss = db.get(SubscriberSettings, subscriber.id)
    if ss is None or not ss.copy_enabled:
        return COPY_OFF_REASON
    return None


def relay_for_subscriber(db: Session, subscriber: User, parent: DiscordAlertSource,
                         batch: list[dict[str, Any]], background, request) -> None:
    """Ingest one trader-channel batch into this subscriber's mirror and act on
    it as the subscriber. Caller commits."""
    from app.api import discord_sources  # noqa: PLC0415 — cycle
    from app.services import discord_edit, discord_ingest  # noqa: PLC0415

    trader = followed_discord_trader(db, subscriber)
    if trader is None or trader.id != parent.user_id:
        return
    mirror = ensure_mirror(db, subscriber, parent)
    ensure_settings(db, subscriber, trader)
    auto = discord_sources._auto_approve(db, subscriber.id, mirror.id)
    report = discord_ingest.ingest_batch(db, mirror, batch, auto_approve=auto, publish=False)

    for msg in report.stored:
        if _is_entry(msg):
            reason = _entry_block_reason(db, subscriber, mirror)
            if reason:
                msg.status = DiscordMessageStatus.IGNORED
                msg.status_reason = reason
                msg.decision = None
                continue
        from app.services import discord_freshness  # noqa: PLC0415

        if discord_freshness.hold_if_stale(msg):
            continue                      # a late entry waits for approval
        if msg.decision is SignalDecision.APPROVED:
            discord_sources._execute_signal(db, subscriber, msg, background, request)

    for msg in report.edited:
        try:
            msg.status_reason = f"Edited alert: {discord_edit.apply_price_edit(db, msg)}"[:480]
        except Exception as exc:  # noqa: BLE001
            msg.status_reason = f"Edited alert: handling failed — {exc}"[:480]
            log.exception("discord: subscriber edit failed for %s", msg.discord_message_id)


class _RelayRequest:
    """Stands in for the HTTP request on the relay thread (audit ip lookups)."""
    headers: dict = {}
    client = None


def _relay_one(subscriber_id: uuid.UUID, parent_id: uuid.UUID, batch: list[dict[str, Any]]) -> None:
    from app.api.discord_sources import _InlineTasks  # noqa: PLC0415
    from app.database import SessionLocal  # noqa: PLC0415

    with SessionLocal() as db:
        try:
            subscriber = db.get(User, subscriber_id)
            parent = db.get(DiscordAlertSource, parent_id)
            if subscriber is None or parent is None:
                return
            relay_for_subscriber(db, subscriber, parent, batch, _InlineTasks(), _RelayRequest())
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("discord: relay to subscriber %s failed", subscriber_id)


def relay_batch(db: Session, parent: DiscordAlertSource, batch: list[dict[str, Any]]) -> int:
    """Hand a trader-channel batch to every follower, each on its own thread and
    session, without waiting — so subscribers trade the alert at the same time as
    the trader. Returns how many relays were started."""
    if not batch or parent.parent_source_id is not None or parent.channel_id == SELF_CHANNEL_ID:
        return 0
    trader = db.get(User, parent.user_id)
    if trader is None or not trader.discord_enabled:
        return 0
    from app.database import SessionLocal  # noqa: PLC0415

    # Mirrors and settings are created up front, committed in a session of
    # their own: two relay threads for one subscriber must never both try to
    # create the same mirror, and the caller's transaction stays its own.
    sub_ids: list[uuid.UUID] = []
    with SessionLocal() as setup:
        try:
            parent_row = setup.get(DiscordAlertSource, parent.id)
            for sub in followers(setup, trader.id):
                ensure_mirror(setup, sub, parent_row)
                ensure_settings(setup, sub, trader)
                sub_ids.append(sub.id)
            setup.commit()
        except Exception:  # noqa: BLE001
            setup.rollback()
            log.exception("discord: preparing subscriber relays for %s failed", parent.id)
            return 0
    for sid in sub_ids:
        _pool.submit(_relay_one, sid, parent.id, list(batch))
    return len(sub_ids)


__all__ = [
    "followed_discord_trader", "has_discord", "followers", "trader_channels",
    "ensure_mirror", "sync_mirrors", "ensure_settings", "relay_for_subscriber",
    "relay_batch",
]
