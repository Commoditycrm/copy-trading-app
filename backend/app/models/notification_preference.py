"""Per-user notification channel preferences.

One row per user. Two master switches (email / sms) gate everything, and a
per-event JSONB map lets a user mute a specific channel for a specific event
without turning that channel off entirely. In-app is always on — it's the
persistent inbox and costs nothing to write.

Channel resolution (see ``channel_enabled``):
  in_app : always True
  email  : master email_enabled AND per-event email flag (default True)
  sms    : master sms_enabled AND per-event sms flag (default True)
           AND the caller has a verified phone (enforced in the service, not
           here — this model doesn't know about the user row).
"""
import uuid

from sqlalchemy import Boolean, ForeignKey, false
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

# Event types that can notify. Keep in sync with the frontend prefs UI and the
# call sites in copy_engine / the fill reconciler.
EVENT_ORDER_FILLED = "order.filled"
EVENT_ORDER_REJECTED = "order.rejected"
NOTIFY_EVENTS = (EVENT_ORDER_FILLED, EVENT_ORDER_REJECTED)


class NotificationPreference(Base, TimestampMixin):
    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Master switches. Email defaults on (we already have the address); SMS
    # defaults off until the user adds + verifies a phone number.
    email_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    sms_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    # Per-event channel overrides:
    #   {"order.filled": {"email": true, "sms": false}, ...}
    # A missing event or channel key defaults to True, so an empty map means
    # "every enabled channel fires for every event".
    event_overrides: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    user = relationship("User", foreign_keys=[user_id])

    def channel_enabled(self, event_type: str, channel: str) -> bool:
        """Whether ``channel`` ("email"|"sms") should fire for ``event_type``,
        combining the master switch with the per-event override. Does NOT check
        phone verification for SMS — the service layer owns that."""
        master = self.email_enabled if channel == "email" else self.sms_enabled
        if not master:
            return False
        override = (self.event_overrides or {}).get(event_type, {})
        return bool(override.get(channel, True))
