"""Global runtime settings — a tiny key/value store for operational flags an
admin can flip at runtime WITHOUT an env change + redeploy.

Read by any process (web + worker) from the shared DB. The env var stays the
DEFAULT; a row here, when present, overrides it. Keep this for a handful of
operational toggles, not per-user config (that lives on the user/trader rows).
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False,
    )
