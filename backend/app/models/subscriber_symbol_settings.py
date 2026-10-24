"""Per-symbol copy settings for a subscriber.

A copy-trading subscriber has ONE set of rules for everything they mirror: a
multiplier, caps, and whatever the trader does with the position. This lets
them say "AAPL is different" — its own size, and its own way out — without a
Discord channel and without disturbing any other symbol.

Shaped like ``DiscordAlertSource.channel_settings`` on purpose. A channel is
already "a scope that overrides some settings and inherits the rest", and that
mechanism (services/discord_channel_settings) is tested and in production; this
is the same mechanism keyed on a SYMBOL instead of a channel, so both read
through one overlay and the exit-ladder engine cannot tell them apart.

``settings`` is sparse: a field is present only when the subscriber overrode it.
That is what makes a partial configuration work — anything absent falls through
to the subscriber's global settings — and it is why this is JSONB rather than
thirty nullable columns, where NULL could not distinguish "not overridden" from
"overridden to nothing", and every new ladder field would need a migration.

The key is the UNDERLYING root ("AAPL"), never a contract. The exit ladder
already keys on the root (``DiscordPositionGuard.symbol``), so a rule for AAPL
covers AAPL shares and every AAPL option without any new matching.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, true
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SubscriberSymbolSettings(Base, TimestampMixin):
    """One symbol a subscriber has customised."""

    __tablename__ = "subscriber_symbol_settings"
    __table_args__ = (
        # One rule per symbol per subscriber. Two would make "which applies?"
        # unanswerable, and the resolver would pick arbitrarily.
        UniqueConstraint("user_id", "symbol", name="uq_subscriber_symbol"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # The underlying root, upper-cased on the way in — "AAPL", never an OCC
    # contract. Short because a root is at most 5-6 characters.
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    # Turned off keeps the configuration but stops it applying, so a subscriber
    # can park a rule without losing what they set up.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true(),
    )

    # Sparse overrides: {field: value}. Absent means "inherit".
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SubscriberSymbolSettings {self.symbol} overrides={len(self.settings or {})}>"
