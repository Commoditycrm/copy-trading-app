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
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.brokers import adapter_for
from app.models.broker_account import BrokerAccount
from app.models.discord_message import DiscordMessage, DiscordMessageStatus, SignalDecision
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderType
from app.models.user import User
from app.schemas.order import PlaceOrderIn
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)


class ExecutionRefused(Exception):
    """A validated reason not to place this order. The message is shown to the
    trader verbatim, so it has to explain itself without reference to code."""


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


def resolve(db: Session, user: User, signal: dict[str, Any]) -> Resolved:
    """Turn a parsed signal into a concrete order, or refuse with a reason.

    Raises :class:`ExecutionRefused` for anything that can't be placed safely.
    """
    resolutions: dict[str, str] = {}

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

    quantity = _resolve_quantity(signal, positions, strike, right, expiry, is_closing, resolutions)
    limit_price = _resolve_limit_price(
        signal, adapter, symbol, strike, right, expiry, side, resolutions
    )

    payload = PlaceOrderIn(
        instrument_type=InstrumentType.OPTION if is_option else InstrumentType.STOCK,
        symbol=symbol,
        side=side,
        # Always LIMIT — see the parser. A market order on a thin option fills
        # at whatever is resting, which can be far from the alerted price.
        order_type=OrderType.LIMIT,
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


def _check_contract_exists(adapter: Any, symbol: str, strike, right, expiry: date) -> None:
    """Verify the option chain actually lists this contract.

    Skipped silently when the adapter can't enumerate contracts — the broker
    would still reject an invalid one, so a missing capability must not block
    otherwise-valid orders.
    """
    if not hasattr(adapter, "list_option_contracts"):
        return
    try:
        contracts = adapter.list_option_contracts(
            underlying=symbol, expiry_gte=expiry, expiry_lte=expiry, limit=2000
        )
    except Exception:  # noqa: BLE001
        # A chain lookup failure is not evidence the contract is bad. Let the
        # broker be the judge rather than refusing a probably-fine order.
        log.warning("discord_execution: chain lookup failed for %s", symbol, exc_info=True)
        return

    if not contracts:
        near = _nearby_expiries(adapter, symbol, expiry)
        hint = f" Nearest expiries: {', '.join(near)}." if near else ""
        raise ExecutionRefused(
            f"{symbol} has no options expiring {expiry} "
            f"({expiry.strftime('%A')}).{hint}"
        )

    want_cp = "C" if right is OptionRight.CALL else "P"
    for c in contracts:
        c_strike = _dec(getattr(c, "strike_price", None))
        c_type = str(getattr(c, "type", "") or "")[:1].upper()
        if c_strike == strike and c_type == want_cp:
            return

    strikes = sorted({
        _dec(getattr(c, "strike_price", None)) for c in contracts
        if str(getattr(c, "type", "") or "")[:1].upper() == want_cp
    } - {None})
    nearest = _nearest(strikes, strike)
    hint = f" Nearest strikes: {', '.join(str(x) for x in nearest)}." if nearest else ""
    raise ExecutionRefused(
        f"{symbol} {expiry} has no ${strike} {want_cp == 'C' and 'call' or 'put'}.{hint}"
    )


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


def _resolve_quantity(signal, positions, strike, right, expiry, is_closing, resolutions) -> Decimal:
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
    "mark_executed", "mark_failed", "resolve",
]
