import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class RetryInterval(str, enum.Enum):
    """How long to wait before retrying a transient-failed mirror order.
    NEVER disables retry entirely — failed orders go straight to REJECTED
    just like before this feature existed (no behaviour change)."""

    NEVER = "never"
    ONE_M = "1m"
    TWO_M = "2m"
    THREE_M = "3m"
    FIVE_M = "5m"


class TraderSettings(Base, TimestampMixin):
    """One row per trader. Master kill switch for outgoing trades."""

    __tablename__ = "trader_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Pause fanout to subscribers. Pure gate — subscribers' own copy_enabled
    # flags are NOT touched when this flips. When True, fanout skips everyone
    # regardless of their preference.
    copy_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Follow-request policy. False (default) = a subscriber must request to
    # follow and the trader approves (the request/approval flow). True =
    # "auto-allow": any subscriber can follow this trader directly, no request
    # or approval needed. Surfaced on the trader's Subscribers page as a
    # dropdown, and consulted by settings.follow_trader's approval gate.
    auto_approve_follows: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )

    # ── Discord trade-alert broadcast (Phase 1) ───────────────────────────────
    # A Discord "Incoming Webhook" URL for the trader's own channel. When set +
    # discord_alerts_enabled, every FILLED order the trader places is posted as
    # an ENTERING/CLOSING card (services/discord_alerts) so their subscribers see
    # it in real time — the Kopyya-native version of the Alertsify feed. Nullable
    # + default OFF so existing traders are unchanged. The URL is a bearer
    # secret (anyone with it can post to the channel); it's stored as-is because
    # it grants no access to Kopyya or the broker, only to that one channel.
    discord_webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    discord_alerts_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )

    # ── Discord alert INBOUND handling ───────────────────────────────────────
    # How parsed alerts from ANY connected Discord channel are handled:
    #
    #   manual — the trader accepts or rejects each one in Order History
    #   auto   — a successful parse is treated as approved, ready for execution
    #
    # One setting for the whole account rather than per channel: it expresses how
    # much the trader trusts automation in general, and splitting it per channel
    # made it easy to leave one feed on auto by accident.
    #
    # Defaults to MANUAL. This decides whether an alert can one day reach a
    # broker unattended, so the safe option has to be the one you get by default.
    #
    # NOTE this is the INBOUND feature, unrelated to discord_webhook_url /
    # discord_alerts_enabled above, which broadcast the trader's own fills OUT.
    discord_execution_mode: Mapped[str] = mapped_column(
        String(10), default="manual", server_default="manual", nullable=False,
    )

    # ── Discord alert sizing ─────────────────────────────────────────────────
    # How many contracts to trade per alert, as a multiple of the alert's own
    # size (which is 1 for the compact formats that state none).
    #
    # Independent of the copy-trading multiplier on SubscriberSettings — a
    # trader can follow someone at 1x while sizing Discord alerts differently,
    # and conflating the two would make one silently change the other.
    #
    # Applies to ENTRIES only. A close always sells the position actually held;
    # multiplying an exit would either strand size or try to sell more than
    # exists.
    discord_quantity_multiplier: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False,
    )
    # How an ENTRY is sized: "contracts" (exactly discord_quantity_multiplier)
    # or "dollars" (as many whole contracts as fit in discord_size_dollars at the
    # price the order will actually pay — the live price for a market entry).
    discord_size_mode: Mapped[str] = mapped_column(
        String(10), default="contracts", server_default="contracts", nullable=False,
    )
    discord_size_dollars: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # Which ONE cap applies when sizing by contracts: "per_contract",
    # "per_order" or "none". NULL (never chosen) = whichever has a value, Max per
    # contract first. Sizing by dollars uses no cap — the amount is the rule.
    discord_size_cap: Mapped[str | None] = mapped_column(String(12), nullable=True)

    # Ceiling on the dollar value of a single Discord order (quantity x price x
    # 100 for options). Unlike SubscriberSettings.max_per_contract — which is
    # display-only — this one is ENFORCED: quantity is reduced to fit, and an
    # alert whose single contract already exceeds it is refused rather than
    # trimmed to zero. NULL = no ceiling.
    discord_max_per_contract: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2), nullable=True,
    )

    # Ceiling on the WHOLE order's value (quantity x price, x100 for options),
    # where discord_max_per_contract caps what ONE contract may cost. The two
    # answer different questions and are deliberately independent: "this
    # contract is too rich for me" is a judgement about the instrument, "this
    # order is too big for me" is a judgement about exposure. An alert can
    # easily pass one and fail the other — ten contracts at $50 is a cheap
    # contract and a $500 order. NULL = no ceiling.
    discord_max_per_order: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2), nullable=True,
    )

    # Trail used when a Discord position's stop is armed by its FIRST sell
    # alert: a positive percent retrace from the best price seen (20 = exit if
    # it gives back 20% of the peak).
    discord_trail_percent: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), default=Decimal("20"), server_default="20", nullable=False,
    )

    # ── Trim ladder ─────────────────────────────────────────────────────────
    # A Discord exit alert works a position down in three steps rather than
    # flattening it. Every threshold here is measured against the position's
    # ORIGINAL entry price, never the live mark, so the ladder doesn't drift as
    # the price moves:
    #
    #   1st alert — sell half, then protect the rest with a stop below entry
    #   2nd alert — sell half of what's left, and move that stop
    #   3rd alert — exit everything left
    #
    # Each rung has its OWN minimum profit and its OWN stop distance, set
    # independently: changing the 1st cannot move the 2nd or 3rd. A rung only
    # sells once the position is up by its gate, but it sets its stop either
    # way — the gate decides whether to SELL, not whether to protect.
    #
    # A gate of 0 means NO minimum rather than "break-even or better", which is
    # what lets the defaults reproduce the ladder's original behaviour exactly:
    # the 1st trim gated at 20% with a stop 25% below entry, the 2nd and 3rd
    # ungated with the remainder held at break-even (a stop 0% below entry IS
    # break-even). Reading 0 as a threshold would refuse exactly the exits a
    # losing position most needs.
    #
    # On the 2nd and 3rd alerts the quantity being sold leaves via a trailing
    # stop when entry was above discord_trim_price_threshold — a cheap contract
    # isn't worth trailing, it's worth being out of.
    #
    # The unsuffixed pair is the FIRST trim; they predate the other two and are
    # left unrenamed so existing traders keep the values they already set.
    discord_trim_profit_gate_pct: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), default=Decimal("20"), server_default="20", nullable=False,
    )
    # SIGNED: the return the stop sits at, relative to entry. -25 is 25% below
    # entry (the usual protective stop), 0 is break-even, +10 is 10% ABOVE
    # entry — a stop that locks in profit, which the old unsigned field could
    # not express at all.
    discord_trim_stop_pct: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), default=Decimal("-25"), server_default="-25", nullable=False,
    )
    discord_trim2_profit_gate_pct: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), default=Decimal("0"), server_default="0", nullable=False,
    )
    discord_trim2_stop_pct: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), default=Decimal("0"), server_default="0", nullable=False,
    )
    discord_trim3_profit_gate_pct: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), default=Decimal("0"), server_default="0", nullable=False,
    )
    discord_trim3_stop_pct: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), default=Decimal("0"), server_default="0", nullable=False,
    )
    # Run the ladder off the PRICE instead of waiting for an alert.
    #
    # Off, a rung fires when its Discord alert arrives and the profit gate is
    # the condition it has to satisfy. On, there is no alert to wait for: the
    # poller watches the position and fires the rung the moment its gate is
    # reached, through exactly the same execution path.
    #
    # A rung whose gate is 0 is NEVER auto-fired. Zero means "no minimum" — it
    # is the right default for an alert-driven rung (sell whenever the author
    # says to) and meaningless without one, because "reached 0% profit" is true
    # the instant a position is up a cent. Auto-firing those would walk rungs
    # 2 and 3 immediately after rung 1 and flatten the position. So auto-trim
    # needs a positive threshold per rung, which is also the only way a trader
    # can say WHERE they want each automatic trim to happen.
    discord_auto_trim: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )

    # Manual exits: Kopyya never sells a position on its own. The channel's exit
    # alerts are recorded but not acted on, auto-trim does not fire and AI
    # trimming leaves the position alone — the trader closes it by hand (the
    # Positions page, or an exit typed into the alert composer). The third
    # choice beside "wait for the alert" and discord_auto_trim; when set it wins.
    discord_manual_exit: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )

    # How much of what is STILL HELD each rung sells. Of the remainder, not of
    # the original position — that is what makes the rungs compose: 50/50/100
    # works a position of 4 down as 2, then 1, then 1, which is what the ladder
    # did before the size was configurable. The defaults are exactly that, so
    # nobody's live ladder changes shape by upgrading.
    discord_trim_qty_pct: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), default=Decimal("50"), server_default="50", nullable=False,
    )
    discord_trim2_qty_pct: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), default=Decimal("50"), server_default="50", nullable=False,
    )
    discord_trim3_qty_pct: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), default=Decimal("100"), server_default="100", nullable=False,
    )

    # Take-profit orders: each trim rests at the broker as a real limit order
    # (paired with its stop where the broker links the two), instead of Kopyya
    # watching the price and selling at market. The fourth exit choice; Manual
    # wins over it, and it wins over discord_auto_trim. On a broker with no
    # linked take-profit/stop pair it behaves as auto-trim does.
    discord_tp_orders: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )

    # ── a ladder of any length ───────────────────────────────────────────────
    # How many trims the ladder has. The first three live in the columns above
    # (so a ladder nobody has touched reads exactly as it always did); trims
    # past the third are in ``discord_extra_trims``, one
    # {"profit_gate_pct", "stop_pct", "qty_pct"} object each, in order.
    # Read them through services/discord_ladder.rungs(), never directly.
    discord_trim_count: Mapped[int] = mapped_column(
        Integer, default=3, server_default="3", nullable=False,
    )
    discord_extra_trims: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), default=list, server_default="[]", nullable=False,
    )
    # "On Fill": where the stop goes the moment the entry fills, as a return
    # from entry (-25 = 25% below). NULL = no stop until the first trim, which
    # is how the ladder behaved before this existed.
    discord_fill_stop_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4), nullable=True)
    # Which stops TRAIL instead of sitting at a fixed level:
    # {"fill": bool, "trims": [bool, ...]} — the On Fill stop and each trim, in
    # ladder order. A trailing stop's value is a give-back from the high since
    # it was set (15 = 15% below the best price), not a return from entry.
    # Missing/false = a fixed stop, as before this existed. Read through
    # services/discord_ladder.
    discord_stop_trails: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True,
    )

    # Entry price above which an exit trails instead of going to market.
    discord_trim_price_threshold: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0.90"), server_default="0.90", nullable=False,
    )
    # Trail as an absolute DOLLAR give-back, not a percent: exit once the price
    # falls this far from its peak.
    discord_trim_trail_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0.25"), server_default="0.25", nullable=False,
    )

    # ── Chasing an entry that didn't fill ───────────────────────────────────
    # A Discord buy is placed at the price the alert named. If the contract moves
    # before we get there, that limit rests and the trade is missed. After
    # discord_reprice_after_seconds an unfilled entry gets ONE more attempt,
    # discord_reprice_pct above the ORIGINAL limit — not above the current ask,
    # so what it can overpay stays bounded by the alert's own price.
    discord_reprice_after_seconds: Mapped[int] = mapped_column(
        Integer, default=30, server_default="30", nullable=False,
    )
    discord_reprice_pct: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), default=Decimal("10"), server_default="10", nullable=False,
    )

    # Whether approved Discord alerts actually reach the broker.
    #
    #   False (default) — PAPER: the full pipeline runs, validation and all, and
    #                     the outcome is recorded, but NOTHING is sent to the
    #                     broker. This is how a parser is proven safe.
    #   True            — LIVE: approved alerts place real orders.
    #
    # Separate from discord_execution_mode on purpose. That decides WHO approves
    # (you, or automatically); this decides whether an approval spends money.
    # A trader experimenting with auto-approve must not discover it was live.
    discord_live_trading: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )

    # ── Which engine manages exits ──────────────────────────────────────────
    # "ladder" — the configured trim ladder (auto-trim, if on, fires its rungs).
    # "ai"     — an OpenRouter model decides each exit; the ladder's automatic
    #            sweep stands down so two engines never sell the same contracts.
    # Either way a Discord author's own exit alert still runs as it always did.
    discord_exit_engine: Mapped[str] = mapped_column(
        String(10), default="ladder", server_default="ladder", nullable=False,
    )
    # "suggest" — every AI decision waits for the trader to approve it.
    # "auto"    — decisions execute as they arrive (still paper unless
    #             discord_live_trading is on).
    discord_ai_mode: Mapped[str] = mapped_column(
        String(10), default="suggest", server_default="suggest", nullable=False,
    )
    discord_ai_model: Mapped[str] = mapped_column(
        String(120), default="anthropic/claude-sonnet-5.5",
        server_default="anthropic/claude-sonnet-5.5", nullable=False,
    )
    # Ask again once the price has moved this far (%) since the last ask...
    discord_ai_move_pct: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), default=Decimal("5"), server_default="5", nullable=False,
    )
    # ...but never more often than this, per position.
    discord_ai_min_interval_s: Mapped[int] = mapped_column(
        Integer, default=60, server_default="60", nullable=False,
    )
    # The trader's own guidance, appended to the model's instructions.
    discord_ai_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)

    user = relationship("User", back_populates="trader_settings")


class SubscriberSettings(Base, TimestampMixin):
    """One row per subscriber. Holds the multiplier, the trader being followed,
    and the subscriber-side kill switch."""

    __tablename__ = "subscriber_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    following_trader_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    copy_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("1.000"), nullable=False)

    # Daily realized-loss kill switch. Stored as a positive amount (e.g. 500 means
    # "stop after $500 loss today"). NULL disables the feature.
    # When today's realized P&L falls below -daily_loss_limit, copy_enabled is
    # auto-flipped to false and an audit + SSE event are emitted.
    daily_loss_limit: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    # Daily realized-PROFIT kill switch — symmetric counterpart to
    # daily_loss_limit. Positive amount (e.g. 500 = "stop after $500 profit
    # today"). NULL disables. When today's realized P&L reaches
    # +daily_profit_limit, copy_enabled flips to false (same path as loss).
    daily_profit_limit: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    # Percentage variants of the loss / profit kill switches — the UI
    # uses these now and the absolute USD columns above are legacy. Each
    # is a percent of the broker's beginning-day balance. pnl_poller
    # computes the dollar threshold each tick as
    # ``beginning_day_balance * pct / 100`` and trips the kill switch on
    # the same realized-P&L breach. Bounds: 0 < pct <= 100. NULL = off.
    daily_loss_limit_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True,
    )
    daily_profit_limit_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True,
    )

    # ── How an OPENING mirror is sized ──────────────────────────────────────
    # "multiplier" (default) — the legacy behaviour: scale the trader's quantity
    #                          by the subscriber's multiplier (0.25x–10x).
    # "dollar_target"        — size each FRESH opening entry to a fixed dollar
    #                          budget instead, independent of the trader's size:
    #                          qty = floor(risk_per_trade_usd / cost-of-one-unit).
    #                          Lets a small follower track a large-size trader at
    #                          a capped risk the 0.25x multiplier floor can't reach
    #                          (e.g. $500/trade behind a $5,000/trade trader).
    # Applies to OPENS only; adds to an already-held position and closes still use
    # the position's locked multiplier so exits can never be stranded. Closes are
    # never resized. A trade whose single unit already costs more than the budget
    # floors to 0 and is skipped (skipped_zero_qty), same as any zero-qty mirror.
    sizing_mode: Mapped[str] = mapped_column(
        String(16), default="multiplier", server_default="multiplier", nullable=False,
    )
    # Dollar budget per copied trade, used only when sizing_mode == "dollar_target".
    # NULL falls back to multiplier sizing (the mode is inert without a budget).
    risk_per_trade_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True,
    )

    # Account-equity floor that triggers FULL LIQUIDATION + copy disable.
    # ABSOLUTE ACCOUNT-VALUE TARGET (not daily, not P&L-based). When the
    # pnl_poller observes broker-reported equity (total account value) >= this
    # value, everything on the subscriber's broker is closed at market AND
    # ``copy_enabled`` flips to False. Fires whenever equity crosses the target,
    # however long that takes (days or weeks). NULL = off. Re-enable is manual
    # only — the contract is "stop until I turn it back on", same as every other
    # limit. Stamped with ``auto_liquidated_at`` when the trigger fires so the
    # Settings page can show "Auto-liquidated at HH:MM".
    auto_liquidation_limit: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2), nullable=True,
    )
    auto_liquidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Set to the UTC timestamp at which a DAILY limit (loss, profit, or
    # max_account_pct_per_day, plus their _pct variants) flipped
    # copy_enabled to False. NULL means "not paused by a daily limit" —
    # either the user manually disabled (we leave them alone), or copy is
    # currently enabled, or the pause already auto-resumed.
    #
    # Auto-resume: on every fanout entry (copy_engine) AND every pnl_poller
    # tick, if this timestamp is set AND its UTC date is < today's UTC
    # date, we flip copy_enabled back to True and clear this stamp. That's
    # how "auto-resume next UTC day" works for daily limits.
    #
    # Auto-liquidation (`auto_liquidation_limit`) deliberately uses a
    # DIFFERENT column (`auto_liquidated_at`) and is NEVER touched by the
    # auto-resume sweep — the account-value-target liquidation stays sticky
    # until the subscriber manually re-enables copy. That's the intentional
    # split: daily limits forgive on the next day, the hard account-value
    # liquidation does not.
    pnl_auto_paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Ceiling on what ONE CONTRACT may cost (premium x 100). ENFORCED in
    # copy_engine.fanout_async: an OPTION open above it is skipped rather than
    # resized. (This was UI-only once and the comment here said so long after it
    # stopped being true.)
    max_per_contract: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2), nullable=True,
    )

    # Ceiling on what the WHOLE mirror may cost (quantity x price, x100 for
    # options), where max_per_contract caps one contract. Independent: a cheap
    # contract can still be a large order once the multiplier has scaled it, so
    # ten contracts at $50 passes a $500 per-contract cap and is a $500 order.
    # Also enforced in fanout_async, and unlike max_per_contract it applies to
    # STOCK mirrors too — an order's value is an order's value. NULL = no cap.
    max_per_order: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2), nullable=True,
    )

    # Per-position TAKE-PROFIT / STOP-LOSS percentages, applied to every
    # open position the subscriber holds. Independent of any TP/SL on
    # the trader's mirrored entry (which subscribers no longer receive —
    # see copy_engine.fanout_async). pnl_poller checks each tick: for
    # every open position, computes `unrealized_pnl / abs(cost_basis) *
    # 100`. If >= position_tp_pct → close that position at market. If
    # <= -position_sl_pct → same. Per-position only — does NOT flip
    # copy_enabled (other positions and new mirrors keep flowing).
    # Numeric(7,2) so a position_tp_pct of 999.99 is representable
    # (1000%+ moonshots happen on options); SL is bounded 0 < pct <= 100
    # by the API layer since you can't lose more than 100% of cost.
    position_tp_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 2), nullable=True,
    )
    position_sl_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True,
    )

    # When True, this subscriber COPIES the trader's per-trade SL/TP instead
    # of using their own position_tp_pct / position_sl_pct. copy_engine
    # records the trader's bracket as a percent distance on each mirrored
    # entry (Order.take_profit_pct / stop_loss_pct), and the bracket
    # emulator re-anchors it onto the subscriber's own fill when the entry
    # fills. While True, position_enforcer SKIPS this subscriber's own
    # per-position TP/SL so the two mechanisms can't double-close a
    # position. Default False preserves the prior behaviour (own per-
    # position TP/SL; trader's bracket stripped from mirrors).
    copy_trader_bracket: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )

    # Percentage of TODAY'S BEGINNING-DAY ACCOUNT BALANCE (Alpaca's
    # ``last_equity`` — equity at yesterday's close) that bounds today's
    # cumulative filled trade NOTIONAL (capital deployed in mirror orders
    # today, both buy + sell, options × 100). When notional crosses
    # ``beginning_day_balance * pct/100``, copy is auto-paused. Stored as
    # the percent value itself (e.g. 50.00 = 50%). Enforced by pnl_poller
    # every 60s. Using day-start balance (not live equity) keeps the
    # dollar threshold FIXED for the trading day — it doesn't drift up
    # on gains or down on losses mid-day. NULL = feature disabled.
    max_account_pct_per_day: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True,
    )

    # DOLLAR variant of the daily trading cap — mutually exclusive with
    # ``max_account_pct_per_day``. The user picks a unit in the UI: "%"
    # writes ``max_account_pct_per_day`` (and NULLs this column), "$"
    # writes this column (and NULLs the pct one). When today's cumulative
    # filled trade NOTIONAL crosses this absolute dollar amount, copy is
    # auto-paused by pnl_poller — same trip path as the pct variant, but
    # the threshold is a fixed dollar value rather than derived from the
    # day-start balance. Numeric(20,2) so it can hold real account-scale
    # dollar figures. NULL = this unit not in use.
    max_account_usd_per_day: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2), nullable=True,
    )

    # Retry policy for transient broker errors. Two separate intervals so a
    # subscriber can be aggressive about closing positions (late close hurts
    # P&L) and conservative about opening (late open is usually fine — skip
    # the trade rather than enter at a worse price). NEVER → no retry, the
    # order goes straight to REJECTED on broker error (pre-retry behaviour).
    retry_interval_open: Mapped[RetryInterval] = mapped_column(
        Enum(RetryInterval, name="retry_interval"),
        default=RetryInterval.NEVER, server_default="never", nullable=False,
    )
    retry_interval_close: Mapped[RetryInterval] = mapped_column(
        Enum(RetryInterval, name="retry_interval"),
        default=RetryInterval.NEVER, server_default="never", nullable=False,
    )
    # How many additional attempts to make after the original failure.
    # 1 = current behaviour (one retry), max 5. Only consulted when
    # retry_interval_open/close is not "never".
    retry_max_attempts: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False,
    )

    # Per-subscriber symbol filters. Both stored as JSONB arrays of
    # uppercase tickers ("AAPL", "TSLA"). copy_engine consults these on
    # every fanout:
    #   - exclusion_list non-empty + trader's symbol IN it  → skip mirror
    #   - inclusion_list non-empty + trader's symbol NOT in → skip mirror
    #   - empty lists                                       → mirror everything
    # Defaults are empty so existing subscribers' behaviour is unchanged
    # after the migration.
    symbol_exclusion_list: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False,
    )
    symbol_inclusion_list: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False,
    )

    # Per-subscriber end-of-day auto-close of SAME-DAY-EXPIRY (0DTE) OPTION
    # positions. When enabled, the worker market-closes this subscriber's 0DTE
    # option positions in the final ``eod_autoclose_minutes`` before the 16:00
    # ET close, AND copy_engine refuses NEW same-day-expiry option mirrors for
    # that same per-subscriber window. OFF by default (opt-in) so existing
    # subscribers' behaviour is unchanged after the migration. ``minutes`` is
    # clamped to 1..30 (see market_hours.clamp_eod_minutes).
    eod_autoclose_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )
    eod_autoclose_minutes: Mapped[int] = mapped_column(
        Integer, default=15, server_default="15", nullable=False,
    )

    # Per-subscriber auto-cancel of a copied order that stays WORKING (unfilled)
    # too long. When enabled, a background scanner cancels this subscriber's
    # mirror orders (entries AND closes) that have been working longer than
    # ``unfilled_timeout_seconds`` at the broker, then notifies them. Stored in
    # SECONDS (the UI offers seconds/minutes and converts). OFF by default
    # (opt-in) so existing subscribers' behaviour is unchanged. Distinct from
    # the retry policy above: retry re-places orders the broker REJECTED; this
    # cancels orders the broker ACCEPTED but that aren't filling.
    unfilled_timeout_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )
    unfilled_timeout_seconds: Mapped[int] = mapped_column(
        Integer, default=60, server_default="60", nullable=False,
    )

    user = relationship("User", back_populates="subscriber_settings", foreign_keys=[user_id])
    following_trader = relationship("User", foreign_keys=[following_trader_id])
