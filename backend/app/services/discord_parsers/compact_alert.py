"""Parser for compact ticker alerts — the terse one-line style.

    $TSLA 375 CALL 0DTE @0.95
    $SPY 764 CALL 0DTE @0.90
    $AAPL 330 CALL 09/04 @1.00
    ✂️ $SPY 769c +361%
    ✂️ $SPY 770c +372%

Two things make this format different from everything above it:

**There is no action word.** Posting a contract with a price IS the buy. So the
action is read from the SHAPE of the line — a contract with an entry price is an
open, a line marked with a scissors emoji is an exit. That inference is safe only
because it's structural; this parser deliberately refuses any line that doesn't
match one of the two shapes rather than reaching for a default.

**Exits state no expiry.** "✂️ $SPY 769c" assumes you know what you're holding.
The expiry is therefore left UNSET with ``expiry_unspecified``, to be resolved
from the open position at execution time. Inventing a date here would be the
exact class of guess that puts on the wrong contract.

Several trades often arrive in one message (a block of exits), so this parser
reads every line and returns all of them.
"""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

from ._util import parse_expiry, to_decimal
from .base import (
    AssetType,
    OptionType,
    OrderKind,
    ParsedMessage,
    ParseResult,
    Parser,
    ParseStatus,
    SignalAction,
    TradeSignal,
)

_NUM = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?"

# These alerts never state a size — the channel's convention is one lot, and the
# platform currently trades exactly one. Kept as a named constant so it's a
# visible policy decision rather than a literal buried in the parser.
DEFAULT_QUANTITY = Decimal(1)

# Exit marker. Channels use scissors for "trim/close"; ✂ and ✂️ differ by a
# variation selector, and both appear in the wild.
_EXIT_MARKERS = ("✂", "\U0001F52A", "\U0001F6D1")

# $TSLA 375 CALL 0DTE @0.95   /   $AAPL 330 CALL 09/04 @1.00
#   AAPL $350 CALL 09/02        (no price — a market entry)
#
# The "$" floats: some channels put it on the ticker, some on the strike, some
# on both. Allow it in either position rather than assuming one house style.
#
# The entry price is OPTIONAL, but then an EXPIRY is required — see _is_entry().
# Without that rule this pattern would also swallow "$TSLA 375c +43%", turning a
# running P&L update into a BUY order.
_ENTRY_RE = re.compile(
    rf"^\s*\$?(?P<symbol>[A-Za-z][A-Za-z0-9.\-]{{0,9}})\s+"
    rf"\$?(?P<strike>{_NUM})\s*(?P<right>CALLS?|PUTS?|C|P)\b\s*"
    rf"(?P<exp>0DTE|\d{{1,2}}[/-]\d{{1,2}}(?:[/-]\d{{2,4}})?)?\s*"
    rf"(?:@\s*\$?(?P<price>{_NUM})\b)?"
    rf"(?P<trailing>[\s,;].*)?$",
    re.IGNORECASE,
)

# ✂️ $SPY 769c +361%   (price optional, percent optional)
_EXIT_RE = re.compile(
    rf"^\s*(?P<marker>[✂\U0001F52A\U0001F6D1]️?)\s*"
    rf"\$?(?P<symbol>[A-Za-z][A-Za-z0-9.\-]{{0,9}})\s+"
    rf"\$?(?P<strike>{_NUM})\s*(?P<right>CALLS?|PUTS?|C|P)\b\s*"
    rf"(?P<exp>0DTE|\d{{1,2}}[/-]\d{{1,2}}(?:[/-]\d{{2,4}})?)?\s*"
    rf"(?:@\s*\$?(?P<price>{_NUM}))?\s*"
    rf"(?:(?P<sign>[+\-−])\s*(?P<pct>{_NUM})\s*%)?"
    rf"(?P<trailing>[\s,;].*)?$",
    re.IGNORECASE,
)


# "$TSLA 375c +43%" — a contract and a percentage, no entry price, no exit
# marker. In practice these are running P&L updates on a position the channel
# already holds; the same contract is posted repeatedly as it moves (+24%, +45%,
# +60%). Turning each into a SELL would fire three exit orders for one position,
# so they are recognised explicitly and NOT traded.
_UPDATE_RE = re.compile(
    rf"^\s*\$?(?P<symbol>[A-Za-z][A-Za-z0-9.\-]{{0,9}})\s+"
    rf"\$?(?P<strike>{_NUM})\s*(?P<right>CALLS?|PUTS?|C|P)\b\s*"
    rf"(?P<sign>[+\-−])\s*(?P<pct>{_NUM})\s*%"
    rf"(?P<trailing>[\s,;].*)?$",
    re.IGNORECASE | re.DOTALL,
)


# "META -> 100%"  /  "NVDA -> -100%"  /  "TSLA → 25%"
#
# A close reported as a symbol and a result, with NO contract details at all.
# The percentage is the trade's P&L, not a fraction to sell — "-100%" only makes
# sense as a loss, since you cannot close minus one hundred percent of a
# position. Which contract it refers to has to come from what the account
# actually holds.
_CLOSE_ARROW_RE = re.compile(
    rf"^\s*\$?(?P<symbol>[A-Za-z][A-Za-z0-9.\-]{{0,9}})\s*"
    rf"(?:->|=>|–>|—>|\u2192|\u27a1)\s*"
    rf"(?P<sign>[+\-−])?\s*(?P<pct>{_NUM})\s*%"
    rf"(?P<trailing>[\s,;].*)?$",
    re.IGNORECASE | re.DOTALL,
)


# "Adding $MSFT 100c @1.90"  /  "Add $TSLA 375c"  /  "Adding $SPY"
#
# An instruction to increase a position the trader ALREADY holds. That's what
# makes the expiry optional here in a way it isn't for a fresh entry: the
# contract is the one already open, so a missing expiry is resolved from the
# position rather than being a parse failure.
#
# Requires an explicit contract or a "$" ticker so prose like "leave room to add
# in case they want a bit more of a bounce" isn't read as an order.
_ADD_RE = re.compile(
    rf"^\s*(?:ADD|ADDING)\s+"
    rf"(?:\$(?P<dsymbol>[A-Za-z][A-Za-z0-9.\-]{{0,9}})|(?P<symbol>[A-Za-z][A-Za-z0-9.\-]{{0,9}}))"
    rf"(?:\s+\$?(?P<strike>{_NUM})\s*(?P<right>CALLS?|PUTS?|C|P)\b)?"
    rf"\s*(?P<exp>0DTE|\d{{1,2}}[/-]\d{{1,2}}(?:[/-]\d{{2,4}})?)?\s*"
    rf"(?:@\s*\$?(?P<price>{_NUM})\b)?"
    rf"(?P<trailing>[\s,;].*)?$",
    re.IGNORECASE,
)


class CompactAlertParser(Parser):
    name = "compact_alert"

    def _lines(self, message: ParsedMessage) -> list[str]:
        return [ln.strip() for ln in message.text.splitlines() if ln.strip()]

    def matches(self, message: ParsedMessage) -> bool:
        # Update lines are claimed too, so parse() can report WHY they aren't
        # traded rather than letting them fall through as generic chatter.
        return any(
            _UPDATE_RE.match(ln)
            or _CLOSE_ARROW_RE.match(ln)
            or (_ADD_RE.match(ln) and _is_add(_ADD_RE.match(ln)))
            or (_has_marker(ln) and _EXIT_RE.match(ln))
            or (_ENTRY_RE.match(ln) and _is_entry(_ENTRY_RE.match(ln)))
            for ln in self._lines(message)
        )

    def parse(self, message: ParsedMessage) -> ParseResult:
        signals: list[TradeSignal] = []
        errors: list[str] = []
        saw_update = False

        for line in self._lines(message):
            if _has_marker(line):
                m = _EXIT_RE.match(line)
                if m:
                    sig, err = self._exit(m, message)
                    (signals.append(sig) if sig else errors.append(err))
                continue
            # Updates are checked FIRST: "$TSLA 375c +43%" would otherwise
            # satisfy the (now price-optional) entry pattern and become a BUY.
            if _UPDATE_RE.match(line):
                saw_update = True
                continue
            m = _ADD_RE.match(line)
            if m and _is_add(m):
                sig, err = self._add(m, message)
                (signals.append(sig) if sig else errors.append(err))
                continue
            m = _CLOSE_ARROW_RE.match(line)
            if m:
                sig, err = self._close_arrow(m)
                (signals.append(sig) if sig else errors.append(err))
                continue
            m = _ENTRY_RE.match(line)
            if m and _is_entry(m):
                sig, err = self._entry(m, message)
                (signals.append(sig) if sig else errors.append(err))

        if signals:
            return ParseResult.parsed_many(signals)
        if saw_update:
            # Deliberately not a trade: acting on a running P&L update would
            # place an order the channel never asked for.
            return ParseResult.ignored(
                "price update on an open position — not an entry or exit"
            )
        if errors:
            # Every candidate line failed for a stated reason — surface the
            # first rather than letting it look like ordinary chatter.
            return ParseResult.invalid(errors[0])
        return ParseResult.ignored("no compact alert on any line")

    # ── line readers ────────────────────────────────────────────────────────

    def _entry(self, m: re.Match, message: ParsedMessage):
        strike = to_decimal(m.group("strike"))
        if strike is None or strike <= 0:
            return None, f"couldn't read the strike in {m.group(0).strip()!r}"

        expiry, unspecified, err = _read_expiry(m.group("exp"), message)
        if err:
            return None, err

        price = to_decimal(m.group("price"))
        return (
            TradeSignal(
                action=SignalAction.BUY,
                asset_type=AssetType.OPTION,
                symbol=m.group("symbol").upper(),
                option_type=_right(m.group("right")),
                strike=strike,
                expiration=expiry,
                expiry_unspecified=unspecified,
                # The format never states size; one lot is the convention.
                quantity=DEFAULT_QUANTITY,
                order_type=OrderKind.LIMIT if price else OrderKind.MARKET,
                limit_price=price,
                source_action="ENTRY",
                parser=self.name,
            ),
            None,
        )

    def _add(self, m: re.Match, message: ParsedMessage):
        """"Adding $MSFT 100c @1.90" — buy more of a position already held.

        Unlike a fresh entry, a missing expiry is fine: the contract is the one
        already open, so it's resolved from the position rather than guessed. A
        bare "Adding $MSFT" leaves the whole contract unresolved, same as a
        symbol-only close.
        """
        symbol = m.group("dsymbol") or m.group("symbol")
        strike = to_decimal(m.group("strike")) if m.group("strike") else None
        right = _right(m.group("right")) if m.group("right") else None

        expiry, unspecified, err = _read_expiry(m.group("exp"), message, allow_missing=True)
        if err:
            return None, err

        price = to_decimal(m.group("price"))
        return (
            TradeSignal(
                action=SignalAction.BUY,
                asset_type=AssetType.OPTION,
                symbol=symbol.upper(),
                option_type=right,
                strike=strike,
                expiration=expiry,
                expiry_unspecified=unspecified,
                # No strike/right stated ⇒ the contract itself has to come from
                # the open position.
                contract_unspecified=strike is None or right is None,
                quantity=DEFAULT_QUANTITY,
                order_type=OrderKind.LIMIT if price else OrderKind.MARKET,
                limit_price=price,
                source_action="ADD",
                parser=self.name,
            ),
            None,
        )

    def _close_arrow(self, m: re.Match):
        """"META -> 100%" — close the position in this symbol.

        Only the symbol is known. Strike, call/put and expiry are all absent, so
        the contract is flagged ``contract_unspecified`` and must be resolved
        from the open position before this could ever be executed. Guessing which
        contract was meant would be the worst possible failure here — selling a
        position the trader never intended to close.
        """
        pct = to_decimal(m.group("pct"))
        if pct is not None and m.group("sign") in ("-", "−"):
            pct = -pct

        return (
            TradeSignal(
                action=SignalAction.SELL,
                # These channels trade options; the position lookup settles it
                # either way, and nothing acts on this field before then.
                asset_type=AssetType.OPTION,
                symbol=m.group("symbol").upper(),
                option_type=None,
                strike=None,
                expiration=None,
                expiry_unspecified=True,
                contract_unspecified=True,
                quantity=None,          # sized from the position, not the alert
                order_type=OrderKind.MARKET,
                pnl_percent=pct,
                position_closed=True,
                source_action="CLOSE",
                parser=self.name,
            ),
            None,
        )

    def _exit(self, m: re.Match, message: ParsedMessage):
        strike = to_decimal(m.group("strike"))
        if strike is None or strike <= 0:
            return None, f"couldn't read the strike in {m.group(0).strip()!r}"

        # An exit alert usually names no expiry — it assumes you know what you
        # hold. Leave it unset and let execution resolve it from the position.
        expiry, unspecified, err = _read_expiry(
            m.group("exp"), message, allow_missing=True
        )
        if err:
            return None, err

        price = to_decimal(m.group("price"))
        pct = to_decimal(m.group("pct"))
        if pct is not None and m.group("sign") in ("-", "−"):
            pct = -pct

        return (
            TradeSignal(
                action=SignalAction.SELL,
                asset_type=AssetType.OPTION,
                symbol=m.group("symbol").upper(),
                option_type=_right(m.group("right")),
                strike=strike,
                expiration=expiry,
                expiry_unspecified=unspecified,
                quantity=DEFAULT_QUANTITY,
                order_type=OrderKind.LIMIT if price else OrderKind.MARKET,
                limit_price=price,
                pnl_percent=pct,
                source_action="TRIM",
                parser=self.name,
            ),
            None,
        )


def _is_entry(m: re.Match) -> bool:
    """Is this contract line actually an instruction to open a position?

    A price makes it unambiguous. Without one, an EXPIRY is what separates a
    real entry ("AAPL $350 CALL 09/02") from a bare mention of a contract — and
    a trailing signed percentage means it's a P&L update, never an entry.
    """
    if not (m.group("price") or m.group("exp")):
        return False
    trailing = (m.group("trailing") or "").strip()
    return not re.match(rf"^[+\-−]\s*(?:{_NUM})\s*%", trailing)


def _is_add(m: re.Match) -> bool:
    """Guard against prose. An add needs either a "$" ticker or an explicit
    contract — "leave room to add in case they want a bounce" has neither."""
    return bool(m.group("dsymbol") or (m.group("strike") and m.group("right")))


def _has_marker(line: str) -> bool:
    return any(line.lstrip().startswith(mark) for mark in _EXIT_MARKERS)


def _right(raw: str) -> OptionType:
    return OptionType.CALL if raw.upper().startswith("C") else OptionType.PUT


def _read_expiry(raw: str | None, message: ParsedMessage, *, allow_missing: bool = False):
    """Returns ``(expiry, unspecified, error)``."""
    if raw and raw.upper() == "0DTE":
        # Expires the day it was posted. Resolved against the message's own
        # timestamp, not today, so re-reading an old alert doesn't move it.
        if message.posted_at is None:
            return None, False, "0DTE alert has no timestamp to resolve the expiry against"
        return message.posted_at.date(), False, None

    if not raw:
        if allow_missing:
            return None, True, None
        return None, False, "the alert has no expiry"

    expiry, err = parse_expiry(raw, posted_at=message.posted_at)
    if err:
        return None, False, err
    return expiry, False, None
