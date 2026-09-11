"""Parser for structured alert cards — the ENTERING / TRIMMING / CLOSING embeds.

Observed live in a real feed:

    title  ENTERING · OKLO $44 CALL · 09/11
    desc   6 @ $1.58 · $948

    title  TRIMMING · META $630 CALL · 09/18
    desc   Sold 3 @ $10.85 · +$630 · +24%
           2 of 5 still open

    title  CLOSING · MU $1,015 CALL · 09/11
    desc   Sold 1 @ $23.05 · +$705 · +44%
           Position closed · total +$1,815 · +38%

These carry EMPTY message content — everything is in the embed. A parser reading
only ``content`` would reject every alert in the feed, which is why this one
works off the embed's title and description.

Because the format is machine-generated it can be read with high confidence,
unlike prose. That confidence is exactly why the checks below are strict: if the
title doesn't match the shape, we hand the message on rather than salvage what
we can.
"""
from __future__ import annotations

import re
from decimal import Decimal

from ._util import parse_expiry, to_decimal
from .base import (
    AssetType,
    OptionType,
    OrderKind,
    ParsedMessage,
    ParseResult,
    Parser,
    SignalAction,
    TradeSignal,
)

# ACTION · SYMBOL $STRIKE CALL|PUT · MM/DD
# The strike allows commas and decimals — "$1,015" and "$227.50" are both real.
_TITLE_RE = re.compile(
    r"^\s*(?P<action>ENTERING|TRIMMING|CLOSING|ADDING)\s*[·\-|]\s*"
    r"(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+"
    r"\$?(?P<strike>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<right>CALL|PUT|C|P)\s*[·\-|]\s*"
    r"(?P<expiry>[0-9]{1,2}[/-][0-9]{1,2}(?:[/-][0-9]{2,4})?)",
    re.IGNORECASE,
)

# Stock variant: no strike/right/expiry.
_TITLE_STOCK_RE = re.compile(
    r"^\s*(?P<action>ENTERING|TRIMMING|CLOSING|ADDING)\s*[·\-|]\s*"
    r"(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s*(?:shares|stock)?\s*$",
    re.IGNORECASE,
)

# "6 @ $1.58"  /  "Sold 3 @ $10.85"  /  "Bought 4 @ $2.36"
_FILL_RE = re.compile(
    r"(?:(?P<verb>sold|bought|buy|sell)\s+)?(?P<qty>\d+(?:\.\d+)?)\s*@\s*"
    r"\$?(?P<price>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# "2 of 5 still open" — a trim states the remaining size explicitly, which is
# far better than inferring it.
_REMAINING_RE = re.compile(r"(?P<left>\d+(?:\.\d+)?)\s+of\s+(?P<total>\d+(?:\.\d+)?)\s+still\s+open",
                           re.IGNORECASE)

_NUM = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?"

# A SIGNED dollar figure is P&L ("+$152", "-$96"); an unsigned one after the
# fill is the notional ("· $285"). The sign is what tells them apart, so it has
# to be part of the match rather than stripped.
_SIGNED_MONEY_RE = re.compile(rf"(?P<sign>[+\-−])\s*\$\s*(?P<amt>{_NUM})")
_SIGNED_PCT_RE = re.compile(rf"(?P<sign>[+\-−])\s*(?P<pct>{_NUM})\s*%")
# Unsigned "· $285" — the fill's total value.
_NOTIONAL_RE = re.compile(rf"[·|]\s*\$\s*(?P<amt>{_NUM})(?!\s*%)")
# "Position closed · total +$1,815 · +38%"
_POSITION_CLOSED_RE = re.compile(r"position\s+closed", re.IGNORECASE)
_TOTAL_RE = re.compile(rf"total\s*(?P<sign>[+\-−])?\s*\$\s*(?P<amt>{_NUM})", re.IGNORECASE)

_OPENING = {"ENTERING", "ADDING"}
_CLOSING = {"TRIMMING", "CLOSING"}


class AlertCardParser(Parser):
    name = "alert_card"

    def _titles(self, message: ParsedMessage) -> list[str]:
        return [(e.get("title") or "").strip() for e in (message.embeds or []) if e.get("title")]

    def matches(self, message: ParsedMessage) -> bool:
        return any(
            _TITLE_RE.match(t) or _TITLE_STOCK_RE.match(t) for t in self._titles(message)
        )

    def parse(self, message: ParsedMessage) -> ParseResult:
        for embed in message.embeds or []:
            title = (embed.get("title") or "").strip()
            m = _TITLE_RE.match(title)
            stock = None if m else _TITLE_STOCK_RE.match(title)
            if not m and not stock:
                continue

            body = "\n".join(
                filter(None, [(embed.get("description") or ""), _fields_text(embed)])
            )
            source_action = (m or stock).group("action").upper()
            action = (
                SignalAction.BUY if source_action in _OPENING else SignalAction.SELL
            )

            qty, price, fill_err = _read_fill(body)
            if fill_err:
                return ParseResult.invalid(fill_err)

            if stock:
                signal = TradeSignal(
                    action=action,
                    asset_type=AssetType.STOCK,
                    symbol=stock.group("symbol").upper(),
                    quantity=qty,
                    order_type=OrderKind.LIMIT if price else OrderKind.MARKET,
                    limit_price=price,
                    source_action=source_action,
                    parser=self.name,
                )
            else:
                strike = to_decimal(m.group("strike"))
                if strike is None or strike <= 0:
                    return ParseResult.invalid(f"couldn't read the strike from {title!r}")

                expiry, exp_err = parse_expiry(
                    m.group("expiry"), posted_at=message.posted_at
                )
                if exp_err:
                    return ParseResult.invalid(exp_err)

                right = m.group("right").upper()
                signal = TradeSignal(
                    action=action,
                    asset_type=AssetType.OPTION,
                    symbol=m.group("symbol").upper(),
                    option_type=OptionType.CALL if right.startswith("C") else OptionType.PUT,
                    strike=strike,
                    expiration=expiry,
                    quantity=qty,
                    order_type=OrderKind.LIMIT if price else OrderKind.MARKET,
                    limit_price=price,
                    source_action=source_action,
                    parser=self.name,
                )

            # A trim closes only part of the position. Recording that explicitly
            # keeps anything downstream from flattening a position the trader
            # still holds.
            if source_action in _CLOSING:
                left = _REMAINING_RE.search(body)
                if left:
                    signal.is_partial_close = True
                    signal.remaining_quantity = to_decimal(left.group("left"))
                    signal.original_quantity = to_decimal(left.group("total"))
                elif source_action == "TRIMMING":
                    # Called a trim but didn't say what's left — flag it rather
                    # than assume the position is closed.
                    signal.is_partial_close = True

            _read_card_figures(signal, body)
            return ParseResult.parsed(signal)

        return ParseResult.ignored("no alert card in this message")


def _fields_text(embed: dict) -> str:
    return "\n".join(
        f"{(f.get('name') or '').strip()} {(f.get('value') or '').strip()}".strip()
        for f in embed.get("fields") or []
    )


def _read_fill(body: str) -> tuple[Decimal | None, Decimal | None, str | None]:
    """Quantity and price from the card body. Returns ``(qty, price, error)``."""
    m = _FILL_RE.search(body or "")
    if not m:
        return None, None, "the alert card has no quantity/price line"
    qty = to_decimal(m.group("qty"))
    price = to_decimal(m.group("price"))
    if qty is None or qty <= 0:
        return None, None, "the alert card has no usable quantity"
    return qty, price, None


def _read_card_figures(signal, body: str) -> None:
    """Pull the rest of what the card states onto the signal.

    Everything here is REPORTED by the alert, never computed by us — if the
    source's arithmetic differs from ours, the source's figure is the one the
    trader saw, and the audit trail should show that.

    Lines look like:
        1 @ $2.85 · $285                             (open: notional)
        Sold 4 @ $2.74 · +$152 · +16%                (close: fill P&L)
        Position closed · total +$1,815 · +38%       (position totals)
        2 of 5 still open                            (handled by the caller)
    """
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    if not lines:
        return

    fill_line = lines[0]

    # P&L on this fill — signed, so it can't be confused with the notional.
    money = _SIGNED_MONEY_RE.search(fill_line)
    if money:
        signal.pnl_amount = _signed(money.group("sign"), money.group("amt"))
        pct = _SIGNED_PCT_RE.search(fill_line)
        if pct:
            signal.pnl_percent = _signed(pct.group("sign"), pct.group("pct"))
    else:
        # No signed figure ⇒ any trailing "· $N" is the fill's total value.
        # Search AFTER the price so "@ $2.85" isn't mistaken for the notional.
        at = fill_line.find("@")
        tail = fill_line[at + 1:] if at >= 0 else fill_line
        notional = _NOTIONAL_RE.search(tail)
        if notional:
            signal.notional = to_decimal(notional.group("amt"))

    for line in lines[1:]:
        if _POSITION_CLOSED_RE.search(line):
            signal.position_closed = True
            # A card that says the position is closed contradicts a partial —
            # trust the explicit statement.
            signal.is_partial_close = False
            signal.remaining_quantity = None
        total = _TOTAL_RE.search(line)
        if total:
            signal.total_pnl_amount = _signed(total.group("sign"), total.group("amt"))
            pct = _SIGNED_PCT_RE.search(line)
            if pct:
                signal.total_pnl_percent = _signed(pct.group("sign"), pct.group("pct"))


def _signed(sign: str | None, amount: str):
    value = to_decimal(amount)
    if value is None:
        return None
    # U+2212 MINUS SIGN — Discord cards use it, and "-" won't match it.
    return -value if sign in ("-", "\u2212") else value
