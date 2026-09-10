import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DiscordMessageStatus(str, enum.Enum):
    """Where a message got to in the pipeline.

    The point of persisting a status per message (rather than only recording the
    ones that became trades) is that "why didn't this alert trade?" has to be
    answerable afterwards. Every terminal state below is a distinct answer.
    """

    # Stored, not yet looked at by the parser.
    RECEIVED = "received"
    # Parsed and deliberately not a trade — chatter, a join notice, a bot reply.
    # Expected and boring; most channels are mostly this.
    IGNORED = "ignored"
    # Parsed into a trade signal that still has to pass validation + risk checks.
    PARSED = "parsed"
    # Looked like a trade but couldn't be turned into one safely — a missing
    # strike, an ambiguous expiry, an unparseable price. NEVER guessed into a
    # trade; this state exists so the failure is visible instead of silent.
    INVALID = "invalid"
    # A real order was created from this message.
    ORDER_CREATED = "order_created"
    # A trade was intended but the order could not be placed (validation, risk
    # limits, or broker rejection). Distinct from INVALID: the signal was fine,
    # the execution wasn't.
    ORDER_FAILED = "order_failed"


class DiscordMessage(Base, TimestampMixin):
    """One message observed in a watched Discord channel.

    Written by the listener intake BEFORE anything tries to interpret it, so the
    original is always on record even if parsing or execution then blows up.
    That ordering is the whole point: an alert that produced a bad trade is only
    debuggable if the raw message that caused it survived.

    ── Duplicate protection (PHASE 7) ────────────────────────────────────────────
    ``uq_discord_message_source_msg`` is the DURABLE guard. Discord's own message
    id is the idempotency key, scoped per source, and the database enforces it —
    so a browser reconnect, page refresh, listener restart, backend retry or
    Redis outage cannot produce a second row, and therefore cannot produce a
    second order. The Redis marker in ``services.discord_ingest`` is only a fast
    path in front of this constraint; this is what actually holds.

    Scoped per SOURCE rather than globally on the Discord message id: two traders
    may legitimately watch the same channel, and each must get their own signal.
    """

    __tablename__ = "discord_messages"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "discord_message_id", name="uq_discord_message_source_msg"
        ),
        # The message list is always read newest-first for one source.
        Index("ix_discord_messages_source_created", "source_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("discord_alert_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalised from the source so a message can be queried by owner without
    # a join, and so ownership survives for as long as the row does.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Discord's own identifiers, read off the rendered DOM node.
    discord_message_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    discord_channel_id: Mapped[str] = mapped_column(String(40), nullable=False)
    discord_server_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Discord's user id, recovered from the avatar URL. NULL for system messages
    # and whenever it couldn't be determined — deliberately null rather than a
    # plausible-looking wrong value.
    author_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # The message text as rendered, with Discord's own chrome (timestamps,
    # "(edited)") stripped. Often EMPTY: alert bots typically put everything in
    # an embed, so ``embeds`` is just as important as this column.
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # When Discord says the message was posted, which is NOT when we saw it —
    # a backlog replay after a restart ingests old messages now. The parser must
    # use this, never created_at, to judge how stale an alert is.
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # JSONB on Postgres (indexable, binary), plain JSON elsewhere. The variant
    # exists so the table can be created on in-memory SQLite in tests — JSONB has
    # no SQLite compilation — without weakening the production column type.
    _JSON = JSONB().with_variant(JSON(), "sqlite")

    attachments: Mapped[list[Any]] = mapped_column(
        _JSON, default=list, server_default="[]", nullable=False
    )
    embeds: Mapped[list[Any]] = mapped_column(
        _JSON, default=list, server_default="[]", nullable=False
    )

    # values_callable tells SQLAlchemy to send enum.VALUE ("received") rather
    # than the member NAME ("RECEIVED"), which is what the Postgres type holds.
    # Same convention as broker_account.BrokerName / follow_request. Without it
    # every insert fails with InvalidTextRepresentation.
    status: Mapped[DiscordMessageStatus] = mapped_column(
        Enum(DiscordMessageStatus, name="discord_message_status",
             values_callable=lambda e: [m.value for m in e]),
        default=DiscordMessageStatus.RECEIVED,
        nullable=False,
        index=True,
    )
    # Why a message ended in IGNORED / INVALID / ORDER_FAILED. Free text aimed at
    # a human reading the audit trail ("no strike in message", "expiry in the
    # past", "insufficient buying power").
    status_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Set once this message produces an order (step 6). Nullable + SET NULL so
    # deleting an order never destroys the message that caused it.
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
