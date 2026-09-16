import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.order import OptionRight


class DiscordPositionGuard(Base, TimestampMixin):
    """Tracks how many SELL alerts a Discord-opened position has received.

    The strategy this exists for:

        BUY          → open the position, remember what it cost
        1st SELL     → if up enough: sell half, stop the rest below entry
        2nd SELL     → sell half of what's left, move that stop to break-even
        3rd SELL     → exit everything left

    Every level is measured from ``entry_price`` — the FIRST fill, held fixed —
    so adding to a position later never moves a stop that is already protecting
    it, and the ladder means the same thing on alert three as on alert one.

    So an exit alert means different things depending on what came before it,
    and that history has to live somewhere. A row here is created by the BUY and
    retired when the position closes.

    ── Why the trail is emulated for options ────────────────────────────────────
    Alpaca's options API rejects trailing-stop orders (see
    services/trailing_stop_close.py). Since Discord alerts are almost entirely
    options, the trail is tracked HERE — ``peak_price`` follows the best price
    seen, and services/discord_trailing_stop.py closes the position when it
    retraces by ``trail_percent``. On instruments where a native trailing stop IS
    available the broker does the work and ``stop_order_id`` records it.
    """

    __tablename__ = "discord_position_guards"
    __table_args__ = (
        # One live guard per contract per trader. A second one would double-count
        # sells and could fire two closes for the same position.
        UniqueConstraint(
            "user_id", "symbol", "option_strike", "option_right", "option_expiry",
            "closed_at", name="uq_discord_guard_contract",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    symbol: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    option_strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    option_right: Mapped[OptionRight | None] = mapped_column(
        String(4), nullable=True
    )
    option_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)

    # How many SELL alerts this position has taken — the rung of the ladder.
    # 0 = just opened, 1 = first trim done, 2 = second trim done, 3+ = closed out.
    # It advances even when a trim does nothing (an alert below the profit gate
    # still counts), so the trader's Nth alert is always read as the Nth trim.
    sell_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    # What the position originally cost: the first BUY's fill price, never
    # re-averaged by later adds. Every percentage in the ladder keys off this.
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)

    # Hard stop on whatever is still held, as an absolute price. Set below entry
    # by the first trim and lifted to break-even by the second. Emulated, like
    # everything else here — Alpaca won't hold a resting stop on an option.
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)

    # A quantity waiting to leave on a trailing stop rather than at market,
    # which is how the 2nd and 3rd trims exit an expensive contract. NULL means
    # nothing is trailing. ``peak_price`` tracks the best price since it armed
    # and ``trail_amount`` is the dollar give-back that triggers the exit.
    trail_qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    trail_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)

    # Trail as a positive percent (20 = exit on a 20% retrace from the peak).
    # Captured when the trail is armed so a later settings change can't silently
    # move the stop on a position already being protected.
    trail_percent: Mapped[Decimal | None] = mapped_column(Numeric(9, 4), nullable=True)
    # Best price seen since arming — what the retrace is measured against.
    peak_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    armed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set only when the BROKER holds a native trailing stop (stocks). NULL means
    # the trail is emulated here.
    stop_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )

    # Retired. Kept (rather than deleted) so the audit trail survives, and so the
    # unique constraint above lets a new position reuse the same contract.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
