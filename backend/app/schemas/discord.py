import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class DiscordSourceIn(BaseModel):
    """Create an inbound Discord alert source.

    In the Follow model, ``channel_id`` is a channel in the TRADER'S OWN server
    that receives a source's announcements via Discord Channel Following. The
    trader supplies their OWN bot token (stored encrypted, never returned); we
    verify the bot can read that follower channel before saving.
    """

    label: str = Field(min_length=1, max_length=120)
    bot_token: str = Field(min_length=20, max_length=200)
    channel_id: str = Field(min_length=5, max_length=40, pattern=r"^\d+$")


class DiscordSourceUpdateIn(BaseModel):
    """Partial update — any field left unset is unchanged."""

    label: str | None = Field(default=None, min_length=1, max_length=120)
    is_enabled: bool | None = None
    # Optional rotation of the bot token (re-verified when present).
    bot_token: str | None = Field(default=None, min_length=20, max_length=200)


class DiscordSourceOut(BaseModel):
    """Public view of a connected source. NEVER includes the bot token."""

    id: uuid.UUID
    label: str
    channel_id: str
    channel_name: str | None
    guild_id: str | None
    is_enabled: bool
    status: str
    last_error: str | None
    created_at: datetime

    # Transient hint from the most recent verify (NOT a stored column):
    #   True  = followed alerts were seen arriving in the channel,
    #   False = channel is readable but no followed messages seen recently,
    #   None  = couldn't tell (e.g. list endpoints don't re-probe live).
    # Set by create/verify routes; absent (None) on plain list.
    receiving_alerts: bool | None = None

    model_config = {"from_attributes": True}
