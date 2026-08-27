"""Snapshot of the positions a user held right before a Sell-All.

Captured by close_all_positions BEFORE it flattens, so the user can later
RE-ENTER the same set (at market, or a chosen % below the exit price). One row
per Sell-All; ``positions`` is a JSON list of the closed positions.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SellAllSnapshot(Base):
    __tablename__ = "sell_all_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    # List of {symbol, instrument_type, quantity (signed), price, option_expiry,
    # option_strike, option_right}. Quantity keeps its sign so Re-Enter can
    # rebuild the same direction (long -> BUY, short -> SELL).
    positions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
