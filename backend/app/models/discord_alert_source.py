import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DiscordAlertSource(Base, TimestampMixin):
    """A trader-connected Discord channel that Kopyaa READS trade alerts FROM
    (INBOUND alert-copying).

    Follow model: ``channel_id`` is a channel in the TRADER'S OWN server that
    receives a source's announcements via Discord Channel Following. We never add
    a bot to the third-party source server (no permission) — the trader Follows
    the source into their own channel and adds our bot there. See
    services/discord_reader.py for the full rationale.

    Deliberately SEPARATE from the OUTBOUND webhook broadcast
    (``TraderSettings.discord_webhook_url`` / ``discord_alerts_enabled``, which
    posts the trader's own fills TO Discord). Different direction, different
    lifecycle, different data — they must not share storage.

    Step 1 (this table) is only the CONNECTION: the trader supplies their OWN
    Discord bot token (stored Fernet-encrypted, like broker credentials) plus the
    follower channel to watch. Reading messages, parsing alerts, placing orders
    and mirroring are later phases and do NOT live here.
    """

    __tablename__ = "discord_alert_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)

    # Fernet-encrypted JSON holding the trader's Discord BOT token
    # ({"bot_token": "..."}). Never stored or returned in plaintext — same
    # pattern as broker_account.encrypted_credentials.
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)

    # Discord identifiers. channel_id is the trader's OWN follower channel we
    # read; guild_id (their server) and channel_name are captured at verify time
    # for display.
    guild_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    channel_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    channel_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Trader on/off for THIS source. Later ingestion only reads enabled sources.
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # "pending" until first verify; "connected" when the bot token + channel
    # check out against Discord; "error" (+ last_error) when they don't.
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
