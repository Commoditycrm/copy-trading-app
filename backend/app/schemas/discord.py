import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class DiscordSourceIn(BaseModel):
    """Create an inbound Discord alert source. The trader supplies their OWN
    Discord bot token (stored encrypted, never returned) plus the channel to
    read. Verified against Discord before it's saved."""

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

    model_config = {"from_attributes": True}
