"""Intake for messages observed by the Discord browser listener (STEP 2).

Boundary between the untrusted listener container and the rest of Kopyaa. The
listener has no database and no broker access; it authenticates with a shared
token and hands us raw messages, which arrive here to be de-duplicated and
queued.

    discord-listener (Playwright + MutationObserver)
        → POST /api/discord-sources/internal/messages
            → ingest_batch()  ← you are here
                → Redis stream "discord:messages:incoming"
                    → (STEP 3) persist  → (STEP 4) parse  → (STEP 5) validate
                        → (STEP 6) existing order pipeline

Nothing here parses a trade or touches an order. A message is data until the
parser has had a look at it, and a *parsed* message is still only a proposal
until validation and risk checks pass — see the phase plan.

Duplicate protection (PHASE 7)
------------------------------
Discord's own message id (a snowflake, read straight off the rendered DOM node)
is the idempotency key, scoped per source: ``source_id + discord_message_id``.
A browser reconnect, page refresh, re-render or listener restart re-observes
messages already in the channel, so duplicates are the NORMAL case, not an edge
case.

The guard is a UNIQUE CONSTRAINT in the database
(``uq_discord_message_source_msg``), not an in-memory or cache check. We attempt
the insert and let Postgres reject the second one. That means a browser
reconnect, listener restart, backend retry, worker retry or Redis outage cannot
produce a second row — and since orders will hang off these rows, cannot produce
a second order.

Scoped per SOURCE, not globally on the message id: two traders may legitimately
watch the same channel and each must get their own signal.

Persist-then-queue, in that order. The row is committed before the message is
handed to the pipeline, so an alert that later causes a bad trade can still be
read back exactly as it arrived. If queueing then fails, the row simply stays at
RECEIVED and can be replayed — the record is never the thing we lose.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.discord_alert_source import DiscordAlertSource
from app.models.discord_message import DiscordMessage, DiscordMessageStatus
from app.services import events
from app.services.redis_client import get_sync_redis

log = logging.getLogger(__name__)

# Redis STREAM, not pub/sub. Pub/sub drops anything published while no consumer
# is attached, which for a trade alert means a silently missed trade whenever the
# parser worker restarts. A stream retains entries and supports consumer groups,
# so step 4's worker can pick up where it left off.
STREAM_KEY = "discord:messages:incoming"

# Bound the stream so a runaway channel can't consume the Redis instance. Well
# above any realistic alert volume; entries are consumed within seconds.
_STREAM_MAXLEN = 10_000

@dataclass
class IngestReport:
    """Outcome of one batch, returned to the listener so it can log/ack."""

    # Stored durably. This is what "we took responsibility for it" means.
    accepted: list[str] = field(default_factory=list)
    # Already on record — the normal outcome of a reconnect or backlog replay.
    duplicates: list[str] = field(default_factory=list)
    # NOT taken: nothing was stored and the message is gone unless re-observed.
    rejected: list[dict[str, str]] = field(default_factory=list)
    # Stored, but couldn't be pushed onto the pipeline queue. Deliberately NOT
    # "rejected": the durable row exists at RECEIVED, so the parser can pick it
    # up from the table. Tracked separately so a Redis outage is visible rather
    # than looking like a clean ingest.
    queue_failed: list[str] = field(default_factory=list)

    @property
    def newest_message_id(self) -> str | None:
        """Highest accepted snowflake. Snowflakes are monotonic by creation
        time, so max() is the newest message — compared as ints because they
        outgrow lexical ordering at 19 digits."""
        if not self.accepted:
            return None
        try:
            return max(self.accepted, key=int)
        except ValueError:
            return self.accepted[-1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": len(self.accepted),
            "duplicates": len(self.duplicates),
            "rejected": self.rejected,
            "queue_failed": len(self.queue_failed),
        }


def _publish(source: DiscordAlertSource, message: dict[str, Any]) -> bool:
    """Queue one message for the parser pipeline. False if Redis rejected it."""
    try:
        get_sync_redis().xadd(
            STREAM_KEY,
            {
                "source_id": str(source.id),
                "user_id": str(source.user_id),
                # The message payload rides as one JSON field: stream fields are
                # flat strings, and attachments/embeds are nested.
                "message": _json(message),
            },
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
        return True
    except Exception:  # noqa: BLE001
        log.warning(
            "discord_ingest: failed to queue message=%s for source=%s",
            message.get("message_id"), source.id, exc_info=True,
        )
        return False


def _json(payload: dict[str, Any]) -> str:
    import json  # noqa: PLC0415

    return json.dumps(payload, default=str, separators=(",", ":"))


def _persist(db: Session, source: DiscordAlertSource, raw: dict[str, Any]) -> DiscordMessage | None:
    """Insert the raw message. Returns None if it's already on record.

    The uniqueness decision is made by the DATABASE, not by a prior SELECT: a
    check-then-insert would still race two concurrent batches carrying the same
    message. We attempt the insert inside a SAVEPOINT and let the constraint
    reject the duplicate, so the outer transaction (and the rest of the batch)
    survives the rollback untouched.
    """
    row = DiscordMessage(
        source_id=source.id,
        user_id=source.user_id,
        discord_message_id=str(raw["message_id"]).strip(),
        discord_channel_id=str(raw.get("channel_id") or source.channel_id),
        discord_server_id=(str(raw["server_id"]) if raw.get("server_id") else None),
        author=(raw.get("author") or None),
        author_id=(raw.get("author_id") or None),
        content=(raw.get("content") or ""),
        posted_at=_parse_ts(raw.get("timestamp")),
        attachments=list(raw.get("attachments") or []),
        embeds=list(raw.get("embeds") or []),
        status=DiscordMessageStatus.RECEIVED,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None
    return row


def ingest_batch(
    db: Session,
    source: DiscordAlertSource,
    messages: Iterable[dict[str, Any]],
) -> IngestReport:
    """Persist a batch from the listener, then queue whatever was new.

    Order matters: the message is stored BEFORE it's handed to the pipeline, so
    the original survives whatever parsing or execution does with it later.

    Caller (the API route) owns the transaction and commits. Never raises on a
    single bad message — one malformed entry is recorded in ``rejected`` and the
    rest of the batch still goes through, because a listener bug must not stall a
    live alert feed.
    """
    report = IngestReport()
    newest_ts: datetime | None = None

    for raw in messages:
        message_id = str(raw.get("message_id") or "").strip()
        if not message_id:
            # No snowflake means no idempotency key, so this message could never
            # be de-duplicated. Reject rather than invent one.
            report.rejected.append({"message_id": "", "reason": "missing_message_id"})
            continue

        row = _persist(db, source, raw)
        if row is None:
            report.duplicates.append(message_id)
            continue

        # Best-effort. The durable record already exists, so a queue failure
        # leaves the row at RECEIVED to be replayed — it is never a lost message,
        # which is why this doesn't roll the insert back.
        if not _publish(source, raw):
            report.queue_failed.append(message_id)

        report.accepted.append(message_id)
        ts = row.posted_at
        if ts and (newest_ts is None or ts > newest_ts):
            newest_ts = ts

    if report.accepted:
        source.last_message_at = newest_ts or datetime.now(timezone.utc)
        newest_id = report.newest_message_id
        if newest_id and _is_newer(newest_id, source.last_seen_message_id):
            source.last_seen_message_id = newest_id
        # A message arriving is the strongest possible proof the watcher is
        # live, so it doubles as a heartbeat.
        source.last_heartbeat_at = datetime.now(timezone.utc)
        _emit(source, "discord.message_received", {"count": len(report.accepted)})

    return report


def _is_newer(candidate: str, current: str | None) -> bool:
    """Snowflake comparison that tolerates junk in either operand."""
    if not current:
        return True
    try:
        return int(candidate) > int(current)
    except ValueError:
        return True


def _parse_ts(value: Any) -> datetime | None:
    """Discord renders ISO-8601 timestamps; anything else is ignored rather
    than trusted, since this value only drives display."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def record_status(
    source: DiscordAlertSource,
    status: str,
    *,
    error: str | None = None,
    channel_name: str | None = None,
    guild_name: str | None = None,
) -> None:
    """Apply a listener-reported connection state to the source row.

    Mirrors ``services.listener_state`` for broker listeners: one place writes
    the status so ``GET /api/discord-sources`` and the SSE pill can't disagree.
    Caller commits.
    """
    source.status = status
    source.last_error = (error or None) if status == "error" else None
    if source.last_error:
        source.last_error = source.last_error[:480]
    source.last_heartbeat_at = datetime.now(timezone.utc)
    # Names are display-only and only known once the channel is actually open,
    # so the listener backfills them opportunistically.
    if channel_name:
        source.channel_name = channel_name[:200]
    if guild_name:
        source.guild_name = guild_name[:200]
    _emit(source, "discord.source_status", {"status": status, "error": source.last_error})


def _emit(source: DiscordAlertSource, event_type: str, extra: dict[str, Any]) -> None:
    """Push a live update to the owner's SSE stream. Best-effort by design —
    ``events.publish`` already swallows Redis failures, and Postgres remains the
    source of truth the UI refetches from."""
    events.publish(
        source.user_id,
        {"type": event_type, "source_id": str(source.id), **extra},
    )


__all__ = [
    "STREAM_KEY",
    "IngestReport",
    "ingest_batch",
    "record_status",
]
