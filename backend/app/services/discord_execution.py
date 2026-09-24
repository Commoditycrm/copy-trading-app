"""Turn an approved Discord alert into a real broker order.

    approved signal → validate → resolve against the account → place → record

Everything here exists to answer one question: is there enough certainty to put
real money behind this message? An alert is free text written by someone else;
by the time it reaches this module it has been parsed, but "parsed" only means
we understood the words. This is where we check the trade is actually placeable.

── Refuse rather than guess ─────────────────────────────────────────────────────
Every check below fails CLOSED. A missing expiry, an unresolvable contract, an
expired option, no position to close, a disconnected broker — each returns a
reason and places nothing. That is deliberate: the cost of skipping a real alert
is a missed trade, while the cost of guessing is a real position in the wrong
contract. Those are not symmetric.

── What this reuses rather than reimplements ───────────────────────────────────
Placement itself goes through ``api.trades._place_trader_order``, the same path
the Trade Panel uses. That brings the advisory-lock duplicate guard, the
app-originated marker, retry scheduling, fill tracking and subscriber fanout for
free — and means a Discord order is indistinguishable from any other order once
placed, which is what makes the rest of the platform work on it unchanged.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.brokers import adapter_for
from app.models.broker_account import BrokerAccount
from app.models.discord_message import DiscordMessage, DiscordMessageStatus, SignalDecision
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderType
from app.models.user import User
from app.schemas.order import PlaceOrderIn
from app.services import market_hours
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)


class ExecutionRefused(Exception):
    """A validated reason not to place this order. The message is shown to the
    trader verbatim, so it has to explain itself without reference to code."""


@dataclass
class Sizing:
    """The trader's Discord sizing policy, resolved by the caller."""

    multiplier: int = 1
    max_per_contract: Decimal | None = None
    max_per_order: Decimal | None = None


@dataclass
class Resolved:
    """A signal turned into something placeable."""

    payload: PlaceOrderIn
    broker_account_id: uuid.UUID
    is_closing: bool
    # What we had to work out ourselves rather than read from the alert, for the
    # audit trail: expiry from a held position, size from a position, price from
    # a live quote.
    resolutions: dict[str, str]
    # Live mid for the contract, when one was available. Not used to price the
    # order — exits go to market — but a trim re-anchors its trailing stop here.
    mark_price: Decimal | None = None
    # What the broker says this position averaged in at. Used only as a fallback
    # reference for a position the trim ladder never saw open.
    position_entry_price: Decimal | None = None


def resolve(
    db: Session, user: User, signal: dict[str, Any], sizing: "Sizing | None" = None
) -> Resolved:
    """Turn a parsed signal into a concrete order, or refuse with a reason.

    Raises :class:`ExecutionRefused` for anything that can't be placed safely.
    """
    resolutions: dict[str, str] = {}
    sizing = sizing or Sizing()

    acct = _broker_account(db, user)
    adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))

    action = (signal.get("action") or "").upper()
    if action not in ("BUY", "SELL"):
        raise ExecutionRefused(f"Unrecognised action {action or '(none)'}.")
    side = OrderSide.BUY if action == "BUY" else OrderSide.SELL

    symbol = (signal.get("symbol") or "").upper()
    if not symbol:
        raise ExecutionRefused("The alert names no symbol.")

    is_option = (signal.get("asset_type") or "OPTION").upper() == "OPTION"
    # A SELL from an alert channel is always an exit — these channels don't
    # short. Getting this wrong is the SELL_TO_OPEN failure: the broker either
    # rejects it or, worse, opens a naked short.
    is_closing = side is OrderSide.SELL

    positions = _positions(adapter, symbol) if is_option else []

    if is_option:
        strike, right, expiry = _resolve_contract(signal, positions, resolutions)
        _check_expiry(expiry)
        # Confirm the contract actually EXISTS before sending. An alert can name
        # a date no option expires on — "10/10" was a Saturday, "10/12" a Monday
        # when MSFT only has Friday weeklies — and the broker's rejection
        # ("asset not found") tells the trader nothing useful. Checking the chain
        # here turns that into a specific, actionable message.
        _check_contract_exists(adapter, symbol, strike, right, expiry)
    else:
        strike = right = expiry = None

    quantity = _resolve_quantity(
        signal, positions, strike, right, expiry, is_closing, sizing, resolutions
    )
    # Exits go to market so they always fill; entries are limit so they never
    # pay through a wide spread.
    #
    # Outside the regular session that inverts: Alpaca rejects option MARKET
    # orders with "options market orders are only allowed during market hours",
    # so an exit alert arriving pre- or post-market would simply fail. There we
    # price a MARKETABLE limit through the book instead — SELL at the bid — which
    # fills like a market order but is accepted. This mirrors what the copy
    # engine already does for subscriber closes (_marketable_option_limit).
    exit_off_session = (
        is_closing and is_option and not market_hours.in_regular_session()
    )
    limit_price = None
    if not is_closing:
        limit_price = _resolve_limit_price(
            signal, adapter, symbol, strike, right, expiry, side, resolutions
        )
    elif exit_off_session:
        # What the contract is worth right now. The broker's own mark on the
        # held position is the most reliable source — Alpaca exposes no option
        # quote endpoint, so _quote() returns nothing there.
        held_now = next(
            (pp for pp in positions
             if pp.option_strike == strike and pp.option_right == right
             and pp.option_expiry == expiry),
            None,
        )
        ref = _dec(getattr(held_now, "current_price", None)) if held_now else None
        if ref is None:
            quote = _quote(adapter, symbol, strike, right, expiry)
            ref = quote[0] if quote else None
        if ref and ref > 0:
            # Priced THROUGH the market so it crosses immediately — a resting
            # exit is worse than a slightly worse fill.
            limit_price = (ref * Decimal("0.90")).quantize(Decimal("0.01"))
            if limit_price <= 0:
                limit_price = Decimal("0.01")
            resolutions["exit"] = (
                f"marketable limit {limit_price} from a {ref} mark "
                f"(outside regular hours)"
            )
        else:
            # Nothing to price against. Leave it a market order and let the
            # broker be the judge — refusing here would block an exit the trader
            # asked for on a technicality we may be wrong about.
            exit_off_session = False
            log.warning(
                "discord_execution: no mark to price an off-session exit for %s",
                symbol,
            )
    # A close is priced at market, but a TRIM still needs a number to move its
    # trailing stop to. Best-effort: a missing quote must not make an exit
    # unplaceable, which is exactly why the order itself doesn't depend on it.
    mark_price: Decimal | None = None
    position_entry_price: Decimal | None = None
    if is_closing:
        held_pos = next(
            (p for p in positions
             if p.option_strike == strike and p.option_right == right
             and p.option_expiry == expiry),
            None,
        )
        if held_pos is not None:
            position_entry_price = _dec(getattr(held_pos, "avg_entry_price", None))
            # The broker's own mark, when it has one, beats a synthesised mid.
            mark_price = _dec(getattr(held_pos, "current_price", None))
        if mark_price is None and is_option and strike:
            quote = _quote(adapter, symbol, strike, right, expiry)
            if quote:
                mark_price = (quote[0] + quote[1]) / Decimal(2)

        # A hand-pinned price wins over anything the broker says. The exit
        # ladder measures the profit gate and anchors its trails against this
        # number, so pinning it is what lets a sell alert be tested against a
        # price you chose rather than whatever the market is doing. Off by
        # default and gated on its own flag — see services/price_override.
        from app.services import price_override  # noqa: PLC0415

        pinned = price_override.get_pin(
            user.id,
            price_override.contract_key(symbol, strike, right, expiry),
        )
        if pinned is not None:
            log.info(
                "discord_execution: using pinned price %s for %s (broker said %s)",
                pinned, symbol, mark_price,
            )
            mark_price = pinned

    # The dollar cap needs the price, so it's applied once both are known.
    if not is_closing:
        # Independent ceilings, checked in their own right: one is about what a
        # contract costs, the other about what the order costs. An order can
        # pass either and fail the other, and neither reads the other's value.
        quantity = _apply_max_per_contract(
            quantity, limit_price, is_option, sizing, resolutions
        )
        quantity = _apply_max_per_order(
            quantity, limit_price, is_option, sizing, resolutions
        )

    payload = PlaceOrderIn(
        instrument_type=InstrumentType.OPTION if is_option else InstrumentType.STOCK,
        symbol=symbol,
        side=side,
        # MARKET to close, LIMIT to open. An alert saying "close this" means
        # get out, not get out at a price — and an unfilled exit is worse than
        # a slightly worse fill.
        order_type=(
            OrderType.LIMIT if (not is_closing or exit_off_session) else OrderType.MARKET
        ),
        quantity=quantity,
        limit_price=limit_price,
        option_expiry=expiry,
        option_strike=strike,
        option_right=right,
    )
    return Resolved(
        payload=payload,
        broker_account_id=acct.id,
        is_closing=is_closing,
        resolutions=resolutions,
        mark_price=mark_price,
        position_entry_price=position_entry_price,
    )


# ── the individual checks ───────────────────────────────────────────────────

def _broker_account(db: Session, user: User) -> BrokerAccount:
    accounts = [
        a for a in db.query(BrokerAccount).filter(BrokerAccount.user_id == user.id).all()
        if a.connection_status == "connected"
    ]
    if not accounts:
        raise ExecutionRefused(
            "No connected broker account. Connect a broker before Discord alerts can trade."
        )
    if len(accounts) > 1:
        # Deliberately refuse rather than pick. Choosing an account on the
        # trader's behalf could place a trade in the wrong one, and an alert
        # carries nothing to disambiguate with.
        raise ExecutionRefused(
            f"{len(accounts)} connected brokers — choose which one Discord alerts should use."
        )
    return accounts[0]


def _positions(adapter: Any, symbol: str) -> list:
    """Held positions in this symbol. A broker hiccup must not be read as
    'no position' — that would turn a close into an opening short."""
    try:
        return [p for p in adapter.get_positions() if (p.symbol or "").upper() == symbol]
    except Exception as exc:  # noqa: BLE001
        raise ExecutionRefused(f"Couldn't read your positions from the broker: {exc}") from exc


def _resolve_contract(signal: dict, positions: list, resolutions: dict):
    """Pin down strike / right / expiry, filling gaps from the open position."""
    strike = _dec(signal.get("strike"))
    right = _right(signal.get("option_type"))
    expiry = _date(signal.get("expiration"))

    if strike is not None and right is not None and expiry is not None:
        return strike, right, expiry

    # Narrow the held positions by whatever the alert DID state.
    candidates = [
        p for p in positions
        if p.option_strike is not None
        and (strike is None or p.option_strike == strike)
        and (right is None or p.option_right == right)
        and (expiry is None or p.option_expiry == expiry)
        and (p.quantity or 0) != 0
    ]
    if not candidates:
        raise ExecutionRefused(
            "The alert doesn't fully identify the contract and you hold no matching "
            "position to resolve it from."
        )
    if len(candidates) > 1:
        # Several open contracts fit. Picking one would be a coin flip on which
        # position to trade.
        raise ExecutionRefused(
            f"The alert matches {len(candidates)} of your open contracts — it doesn't say which."
        )

    held = candidates[0]
    if strike is None:
        resolutions["strike"] = f"{held.option_strike} (from open position)"
    if right is None:
        resolutions["option_type"] = f"{held.option_right.value} (from open position)"
    if expiry is None:
        resolutions["expiration"] = f"{held.option_expiry} (from open position)"
    return held.option_strike, held.option_right, held.option_expiry


# Index options are FILED under the index root but TRADE under their own.
# Alpaca lists SPXW260916C07585000 in SPX's chain, not SPXW's — asking for the
# SPXW chain returns nothing at all, which reads as "this contract doesn't
# exist" when in fact only the lookup key was wrong. The OCC symbol we place
# with is unaffected: it keeps the root the alert used, which is the one the
# contract actually carries.
_CHAIN_ROOTS = {
    "SPXW": "SPX",      # weekly S&P 500
    "NDXP": "NDX",      # PM-settled Nasdaq-100
    "RUTW": "RUT",      # weekly Russell 2000
    "VIXW": "VIX",      # weekly VIX
}


def _chain_root(symbol: str) -> str:
    """The root to look a chain up under, which is not always the trading root."""
    return _CHAIN_ROOTS.get(symbol.upper(), symbol.upper())


def _check_contract_exists(adapter: Any, symbol: str, strike, right, expiry: date) -> None:
    """Verify the option chain actually lists this contract.

    Skipped silently when the adapter can't enumerate contracts — the broker
    would still reject an invalid one, so a missing capability must not block
    otherwise-valid orders.
    """
    if not hasattr(adapter, "list_option_contracts"):
        return
    root = _chain_root(symbol)
    try:
        contracts = adapter.list_option_contracts(
            underlying=root, expiry_gte=expiry, expiry_lte=expiry, limit=2000
        )
    except Exception:  # noqa: BLE001
        # A chain lookup failure is not evidence the contract is bad. Let the
        # broker be the judge rather than refusing a probably-fine order.
        log.warning("discord_execution: chain lookup failed for %s", symbol, exc_info=True)
        return

    if not contracts:
        near = _nearby_expiries(adapter, root, expiry)
        hint = f" Nearest expiries: {', '.join(near)}." if near else ""
        raise ExecutionRefused(
            f"{symbol} has no options expiring {expiry} "
            f"({expiry.strftime('%A')}).{hint}"
        )

    want_cp = "C" if right is OptionRight.CALL else "P"
    for c in contracts:
        c_strike = _dec(getattr(c, "strike_price", None))
        if c_strike == strike and _contract_type(c) == want_cp:
            return

    strikes = sorted({
        _dec(getattr(c, "strike_price", None)) for c in contracts
        if _contract_type(c) == want_cp
    } - {None})
    nearest = _nearest(strikes, strike)
    hint = f" Nearest strikes: {', '.join(str(x) for x in nearest)}." if nearest else ""
    raise ExecutionRefused(
        f"{symbol} {expiry} has no ${strike} {want_cp == 'C' and 'call' or 'put'}.{hint}"
    )


def _contract_type(contract: Any) -> str:
    """"C" or "P" for an option contract from the chain.

    Alpaca returns an enum whose str() is "ContractType.PUT", so reading the
    first character of str() classified EVERY contract as a call — puts matched
    nothing and were rejected, while calls matched anything at the right strike
    and skipped the call/put check entirely. Prefer .value, which is "call"/
    "put", and fall back to the raw value for adapters that return a plain
    string.
    """
    raw = getattr(contract, "type", "") or ""
    value = getattr(raw, "value", raw)
    return str(value)[:1].upper()


def _nearby_expiries(adapter: Any, symbol: str, wanted: date) -> list[str]:
    """A few real expiries around the one asked for, to make the refusal useful."""
    from datetime import timedelta  # noqa: PLC0415

    try:
        contracts = adapter.list_option_contracts(
            underlying=symbol,
            expiry_gte=wanted - timedelta(days=10),
            expiry_lte=wanted + timedelta(days=10),
            limit=2000,
        )
    except Exception:  # noqa: BLE001
        return []
    found = sorted({
        str(getattr(c, "expiration_date", "")) for c in contracts
        if getattr(c, "expiration_date", None)
    })
    return found[:4]


def _nearest(values: list, target, count: int = 3) -> list:
    if not values or target is None:
        return values[:count]
    return sorted(values, key=lambda v: abs(v - target))[:count]


def _check_expiry(expiry: date | None) -> None:
    if expiry is None:
        raise ExecutionRefused("No expiry could be determined for this contract.")
    today = datetime.now(timezone.utc).date()
    if expiry < today:
        raise ExecutionRefused(f"That contract expired on {expiry}.")


def _resolve_quantity(
    signal, positions, strike, right, expiry, is_closing, sizing, resolutions
) -> Decimal:
    """How many contracts.

    A CLOSE always sizes from the position held, never from the alert — the
    author's size is theirs, not yours. Selling fewer than you hold strands the
    remainder; selling more is rejected or opens a short.
    """
    if is_closing:
        held = next(
            (p for p in positions
             if p.option_strike == strike and p.option_right == right
             and p.option_expiry == expiry),
            None,
        )
        qty = abs(Decimal(str(held.quantity))) if held is not None else Decimal(0)
        if qty <= 0:
            raise ExecutionRefused("You hold no position in that contract to close.")
        resolutions["quantity"] = f"{qty} (your full position)"
        return qty

    qty = _dec(signal.get("quantity"))
    if qty is None or qty <= 0:
        raise ExecutionRefused("The alert states no quantity.")

    # Scale the ENTRY. Closes returned above and are never multiplied — an exit
    # sells what is held, whatever the alert or the multiplier say.
    multiplier = max(1, int(sizing.multiplier or 1))
    scaled = qty * multiplier if multiplier > 1 else qty
    note = f"{qty} x {multiplier} multiplier" if multiplier > 1 else None

    # The author called this one "light" / "not heavy": take half the size we
    # otherwise would. Applied AFTER the multiplier, so it halves what would
    # actually have been placed — a size of 4 becomes 2, which is what the
    # instruction means. Halving the alert's own quantity first would let the
    # multiplier scale it straight back up.
    if signal.get("half_size"):
        halved = (scaled / Decimal(2)).to_integral_value(rounding=ROUND_FLOOR)
        # Never round down to nothing. You cannot buy half a contract, and
        # dropping the trade entirely is not what "smaller" asks for — so a
        # single contract stays a single contract.
        halved = max(halved, Decimal(1))
        note = f"{note} then halved (light)" if note else f"half of {scaled} (light)"
        scaled = halved

    if note:
        resolutions["quantity"] = f"{scaled} ({note})"
    return scaled


def _apply_max_per_contract(qty, limit_price, is_option, sizing, resolutions) -> Decimal:
    """Skip an entry whose contract costs more than the trader's ceiling.

    Mirrors SubscriberSettings.max_per_contract exactly: the test is on a SINGLE
    contract's value (premium x 100), and failing it skips the entry outright
    rather than trimming quantity to fit.

    Skipping rather than trimming is the deliberate part. A limit like this says
    "contracts this expensive aren't for me" — a size judgement, not a budget to
    spend down. Trimming would quietly take the trade anyway at a size the
    trader never chose.

    Options only, and never applied to a close: you must always be able to exit
    a position you already hold.
    """
    cap = sizing.max_per_contract
    if cap is None or cap <= 0 or not is_option or limit_price is None or limit_price <= 0:
        return qty

    per_contract = limit_price * Decimal(100)
    if per_contract > cap:
        raise ExecutionRefused(
            f"A single contract is worth ${per_contract:.2f}, above your "
            f"${cap:.2f} max per contract."
        )
    return qty


def cancel_unfilled_entries(
    db: Session, user: User, *,
    symbol: str,
    instrument_type: "InstrumentType | None" = None,
    strike: "Decimal | None" = None,
    right: "OptionRight | None" = None,
    expiry: "date | None" = None,
) -> list[uuid.UUID]:
    """Cancel this contract's still-working, unfilled Discord ENTRY orders.

    An exit alert means the alert cycle has moved on to getting OUT. An entry
    that never filled — even after the +10% retry — is now a bid for a position
    the trader is already exiting, and leaving it resting means it can still
    fill later into a trade nobody wants: bought at the top of a move whose exit
    signal has already been given, with no rung of the ladder left to protect it.

    Matched on what the ALERT ACTUALLY STATES, not on a fully resolved contract.
    That distinction is the whole point: an exit alert routinely names no expiry
    ("$NVDA 225c +25%") and is normally resolved from the open position — but
    when the entry never filled there IS no position, so resolution fails and a
    contract-complete match would never find the very order this exists to
    cancel. Every field left None widens the match rather than requiring NULL.

    Only UNFILLED entries. A partially filled one is a real position and belongs
    to the trim ladder, not here.

    In practice this only ever fires on the FIRST exit alert for a contract: it
    cancels what it finds, so by the second there is nothing left to cancel.

    Returns the ids cancelled, so the caller can cascade to subscriber mirrors —
    they are resting on the same stale bid.
    """
    from app.models.broker_account import BrokerAccount  # noqa: PLC0415
    from app.models.order import Order, OrderStatus  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    if not symbol:
        return []
    working = (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED)
    where = [
        Order.user_id == user.id,
        Order.parent_order_id.is_(None),          # the trader's own, not a mirror
        Order.symbol == symbol.upper(),
        Order.side == OrderSide.BUY,
        Order.is_closing.is_(False),
        Order.status.in_(working),
    ]
    # Narrow by each field the alert named, and only those.
    if instrument_type is not None:
        where.append(Order.instrument_type == instrument_type)
    if strike is not None:
        where.append(Order.option_strike == strike)
    if right is not None:
        where.append(Order.option_right == right)
    if expiry is not None:
        where.append(Order.option_expiry == expiry)
    entries = list(db.execute(select(Order).where(*where)).scalars())

    cancelled: list[uuid.UUID] = []
    for entry in entries:
        if (entry.filled_quantity or Decimal(0)) > 0:
            continue                                   # a real position now
        if entry.broker_order_id:
            acct = db.get(BrokerAccount, entry.broker_account_id)
            if acct is not None:
                try:
                    adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
                    adapter.cancel_order(entry.broker_order_id)
                except Exception:  # noqa: BLE001
                    # Already gone at the broker is the usual reason, and it
                    # means what we wanted. Anything else is still better
                    # recorded as cancelled than left looking live to us.
                    log.warning(
                        "discord: could not cancel stale entry %s at the broker",
                        entry.id, exc_info=True,
                    )
        entry.status = OrderStatus.CANCELED
        entry.closed_at = datetime.now(timezone.utc)
        cancelled.append(entry.id)
        log.info(
            "discord: cancelled unfilled entry %s for %s — an exit alert arrived",
            entry.id, entry.symbol,
        )
    if cancelled:
        db.commit()
    return cancelled


def cancel_stale_entries_for_signal(db: Session, user: User, signal: dict) -> list[uuid.UUID]:
    """Cancel the unfilled entries an exit alert supersedes, from the RAW signal.

    Called BEFORE :func:`resolve`, and that order is the whole fix. An exit
    alert usually names only part of the contract ("$NVDA 225c +25%") and
    resolve() fills the rest in from the open position — so when the entry never
    filled, resolve() refuses ("you hold no matching position") and anything
    downstream of it never runs. Cancelling from a resolved payload therefore
    could not fire in the one case it exists for: the entry is still resting
    precisely BECAUSE there is no position yet.

    Matching only on the fields the alert states can cancel more than one
    resting entry when several expiries of the same strike are open at once.
    That is the right trade: these channels work one contract at a time, and a
    stale bid left live is the worse outcome.

    No-ops on anything that isn't a SELL. Never a close of a close.
    """
    if (signal.get("action") or "").upper() != "SELL":
        return []
    symbol = (signal.get("symbol") or "").upper()
    if not symbol:
        return []
    is_option = (signal.get("asset_type") or "OPTION").upper() == "OPTION"
    return cancel_unfilled_entries(
        db, user,
        symbol=symbol,
        instrument_type=InstrumentType.OPTION if is_option else InstrumentType.STOCK,
        strike=_dec(signal.get("strike")) if is_option else None,
        right=_right(signal.get("option_type")) if is_option else None,
        expiry=_date(signal.get("expiration")) if is_option else None,
    )


def order_value(qty: Decimal, limit_price: Decimal, is_option: bool) -> Decimal:
    """What this order costs: quantity x price, x100 for an option contract."""
    value = qty * limit_price
    return value * Decimal(100) if is_option else value


def _apply_max_per_order(qty, limit_price, is_option, sizing, resolutions) -> Decimal:
    """Skip an entry whose TOTAL cost is above the trader's ceiling.

    Distinct from _apply_max_per_contract, and deliberately so: that one asks
    what a single contract costs, this one asks what the whole order costs. Ten
    contracts at $50 is a cheap contract and a $500 order, so an alert can pass
    either check and fail the other. Neither reads the other's value.

    Skipped rather than trimmed, for the same reason as the per-contract cap: a
    ceiling like this says how much the trader is willing to put into ONE alert,
    not a budget to spend down. Trimming would take the trade anyway at a size
    they never chose.

    Applies to stocks as well as options — an order's value is an order's value
    — and never to a close: you must always be able to exit what you hold.
    """
    cap = sizing.max_per_order
    if cap is None or cap <= 0 or limit_price is None or limit_price <= 0:
        return qty
    if qty is None or qty <= 0:
        return qty

    total = order_value(Decimal(str(qty)), limit_price, is_option)
    if total > cap:
        raise ExecutionRefused(
            f"This order is worth ${total:.2f}, above your "
            f"${cap:.2f} max per order."
        )
    return qty


def _resolve_limit_price(signal, adapter, symbol, strike, right, expiry, side, resolutions):
    """The limit price: the alert's, or derived from the live quote.

    Orders are always LIMIT, so a price is mandatory. When the alert doesn't
    state one (every close, and some entries) it has to come from the market —
    never from a guess.
    """
    stated = _dec(signal.get("limit_price"))
    if stated is not None and stated > 0:
        return stated

    quote = _quote(adapter, symbol, strike, right, expiry)
    if quote is None:
        raise ExecutionRefused(
            "The alert states no price and no live quote is available for that contract."
        )
    bid, ask = quote
    # Price THROUGH the spread so a limit still fills like a market order, but
    # with a cap. Same reasoning as copy_engine._marketable_option_limit.
    price = (ask if side is OrderSide.BUY else bid).quantize(Decimal("0.01"))
    if price <= 0:
        raise ExecutionRefused("The live quote for that contract is unusable (zero/!).")
    resolutions["limit_price"] = f"{price} (marketable limit from the live quote)"
    return price


def _quote(adapter, symbol, strike, right, expiry):
    """(bid, ask) for the contract, or None. Never raises — a missing quote is
    a refusal, not a crash."""
    try:
        from app.brokers.alpaca import build_occ_symbol  # noqa: PLC0415

        occ = build_occ_symbol(symbol, expiry, strike, right) if strike else symbol
        snap = adapter.get_option_quote(occ) if hasattr(adapter, "get_option_quote") else None
        if not snap:
            return None
        bid, ask = _dec(snap.get("bid")), _dec(snap.get("ask"))
        return (bid, ask) if bid and ask else None
    except Exception:  # noqa: BLE001
        log.warning("discord_execution: quote lookup failed for %s", symbol, exc_info=True)
        return None


# ── small helpers ───────────────────────────────────────────────────────────

def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _right(v) -> OptionRight | None:
    if not v:
        return None
    return OptionRight.CALL if str(v).upper().startswith("C") else OptionRight.PUT


def _date(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def mark_executed(msg: DiscordMessage, order_id: uuid.UUID) -> None:
    msg.status = DiscordMessageStatus.ORDER_CREATED
    msg.order_id = order_id
    msg.status_reason = None


def mark_failed(msg: DiscordMessage, reason: str) -> None:
    msg.status = DiscordMessageStatus.ORDER_FAILED
    msg.status_reason = reason[:480]


def already_executed(msg: DiscordMessage) -> bool:
    """One alert, one order. The decision endpoint and the auto path can both
    reach an approved alert, and a retry must never place a second trade."""
    return msg.order_id is not None or msg.status is DiscordMessageStatus.ORDER_CREATED


__all__ = [
    "ExecutionRefused", "Resolved", "already_executed",
    "cancel_stale_entries_for_signal", "cancel_unfilled_entries",
    "mark_executed", "mark_failed", "resolve",
]
