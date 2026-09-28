import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class UserUiPrefs(Base, TimestampMixin):
    """Per-user UI preferences that follow the user across devices. Currently
    holds per-table column config (visibility, order, widths) but is a general
    bucket so future UI prefs need no new table.

    Applies to every user type (trader, subscriber, admin), so it's keyed on the
    user rather than living in TraderSettings.

    ``column_prefs`` shape::

        { "<tableId>": { "order": ["colId", ...],
                          "hidden": ["colId", ...],
                          "widths": {"colId": 120} } }
    """

    __tablename__ = "user_ui_prefs"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    column_prefs: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
