import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class DiscordAccount(Base, TimestampMixin):
    """A Discord account a trader has connected, and the session that proves it.

    The session authenticates an ACCOUNT, not a channel — so it lives here and
    every channel that account can read points at it. Connect once, then add as
    many channels as you like without signing in again.

    This replaces holding the session on each source, which forced a fresh
    sign-in per channel and opened a separate Discord session for each one, even
    when they were all the same account.

    ``encrypted_session`` is the Fernet-encrypted Playwright storage state
    captured on the trader's own machine by the Kopyaa Connector. Same treatment
    as broker credentials: never returned to the frontend, never logged, and
    handed out only to the listener over its authenticated internal endpoint.
    """

    __tablename__ = "discord_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # What to call this account in the UI. Defaults to something generic and is
    # replaced with the real Discord username once the listener has read it.
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="Discord account")
    # Discord's own username/handle, backfilled by the listener after sign-in so
    # a trader with two accounts can tell them apart.
    discord_username: Mapped[str | None] = mapped_column(String(120), nullable=True)

    encrypted_session: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # needs_login — no usable session (never connected, or Discord signed it out)
    # connected    — a session is stored and the listener is using it
    # error        — the session failed; see last_error
    status: Mapped[str] = mapped_column(String(20), default="needs_login", nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Channels read with this account. SET NULL rather than CASCADE: removing an
    # account must not delete the channels (and their message history) with it —
    # they fall back to "needs an account" and can be re-pointed.
    sources = relationship(
        "DiscordAlertSource", back_populates="account", passive_deletes=True
    )
