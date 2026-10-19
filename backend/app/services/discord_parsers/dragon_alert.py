"""Parser for the DRAGON channel's alerts.

Observed in the channel:

    $MSFT 550 CALL 11/13 @ 17.00          entry
    $IBIT 49 CALL 10/16 @ 1.52            entry
    $WMT 111 CALL 9/25 @ @ 0.51           entry — the "@" typed twice
    $MSFT @ 21.10 | 24% TRIMMING          trim
    $CRWV @ 1.29 | 20%                    trim — no word, the shape says it
    $IBIT @ 1.68 | 10% FREE MONEY         trim
    $WMT @ 0.71 | 40%                     trim
    $WMT UP 100% AT 1.02                  trim — word order varies

Two shapes, both always opening with a "$" ticker:

  * entry — ticker, strike, CALL/PUT, expiry, "@" price. A BUY of one lot at
    the stated limit, the same as a compact-alert entry.
  * trim  — ticker, NO contract, and a percentage anywhere on the line; the
    price ("@ x" / "AT x") and any words are optional. The percentage alone
    makes it a trim — "TRIMMING" is not needed — and is the trade's RESULT
    (17.00 -> 21.10 is +24%), never a fraction to sell. The contract is
    resolved at execution from what is held in that ticker: this channel's own
    latest contract first (``channel_contract_first``), else the one open
    position in it. The exit ladder decides the size. "OUT", "CLOSED",
    "STOPPED" … on the line make it a full close instead.

Why its own parser rather than another pattern in compact_alert: without it,
the other parsers read this channel WRONG, not just partially. "$MSFT @ 21.10 |
24% TRIMMING" was taken by the free-text parser as a sell of MSFT SHARES; the
two trims with no action word were ignored; "@ @ 0.51" lost its price and
would have bid off the live quote.

The trims are also read on the Self channel (``ParsedMessage.self_channel``)
— an alert typed into the Discord popup — where "$MSFT @ 21.10 | 24% TRIMMING"
otherwise reads as a sell of MSFT SHARES and fails against the option held.

Scoped to the channel by name (``ParsedMessage.channel`` — the channel name,
the trader's label and the server name, any of which may say "dragon"): on every other
channel this parser never matches, so nothing that already parses changes.
A Dragon message in neither shape is IGNORED here and falls through to the
other parsers, as it did before.
"""
from __future__ import annotations

import re
from decimal import Decimal

from ._util import to_decimal
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
from .compact_alert import _read_expiry

# A channel whose name or label contains this is the Dragon channel:
# "dragon", "🐉dragon-alerts", "Dragon Trades" all qualify.
CHANNEL_KEYWORD = "dragon"

# The channel's convention is one lot; it never states a size.
DEFAULT_QUANTITY = Decimal(1)

_NUM = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+"
# One or more "@" — "@ @ 0.51" is the same price as "@ 0.51".
_AT = r"(?:@\s*)+"
_SYMBOL = r"\$(?P<symbol>[A-Za-z][A-Za-z0-9.]{0,9})"

# $MSFT 550 CALL 11/13 @ 17.00
_ENTRY_RE = re.compile(
    rf"^\s*{_SYMBOL}\s+"
    rf"\$?(?P<strike>{_NUM})\s*(?P<right>CALLS?|PUTS?|C|P)\s+"
    rf"(?P<exp>(?:\d{{1,2}}|O)DTE|\d{{1,2}}[/-]\d{{1,2}}(?:[/-]\d{{2,4}})?)\s*"
    rf"{_AT}\$?(?P<price>{_NUM})(?![\d.])"
    rf"(?P<trailing>.*)$",
    re.IGNORECASE,
)

# A trim is any line that opens with a "$" ticker, names NO contract, and
# states a percentage — whatever the word order:
#
#   $MSFT @ 21.10 | 24% TRIMMING
#   $WMT @ 0.71 | 40%
#   $WMT UP 100% AT 1.02
#
# The percentage alone makes it a trim; "TRIMMING" is not required.
_TRIM_LINE_RE = re.compile(rf"^\s*{_SYMBOL}(?![A-Za-z0-9])(?P<rest>.*)$", re.IGNORECASE)
_CONTRACT_RE = re.compile(rf"(?:^|\s)\$?(?:{_NUM})\s*(?:CALLS?|PUTS?|C|P)\b", re.IGNORECASE)
_PCT_RE = re.compile(rf"(?P<sign>[+\-−])?\s*(?P<pct>{_NUM})\s*%")
# "@ 21.10", "@ @ 0.71", "AT 1.02".
_TRIM_PRICE_RE = re.compile(rf"(?:(?:@\s*)+|\bAT\s+)\$?(?P<price>{_NUM})(?![\d.%])", re.IGNORECASE)
# "$WMT DOWN 30% AT 0.40": a loss, written as a word rather than a sign.
_DOWN_RE = re.compile(r"\bdown\b", re.IGNORECASE)

# Words after a trim's percentage that mean the author is OUT, not trimming.
_OUT_RE = re.compile(
    r"\b(?:all\s+out|out|closed?|closing|exit(?:ed)?|sold\s+all|stopped(?:\s+out)?|cut)\b",
    re.IGNORECASE,
)

# "@here", "<@123>", "@Dragon" — pings, never a price.
_MENTION_RE = re.compile(r"<[@#][!&]?\d+>|@[A-Za-z_][\w.\-]*")


def is_dragon_channel(message: ParsedMessage) -> bool:
    return CHANNEL_KEYWORD in (message.channel or "").lower()


def _lines(message: ParsedMessage) -> list[str]:
    return [
        " ".join(_MENTION_RE.sub(" ", raw).split())
        for raw in message.text.splitlines()
        if raw.strip()
    ]


def _right(raw: str) -> OptionType:
    return OptionType.CALL if raw.upper().startswith("C") else OptionType.PUT


def _is_entry(m: re.Match) -> bool:
    # A trailing signed percentage is a P&L update on the contract, not a buy.
    return not re.match(rf"^\s*[+\-−]\s*(?:{_NUM})\s*%", m.group("trailing") or "")


def _trim_match(line: str) -> re.Match | None:
    """The ticker line of a trim, or None. ``rest`` holds everything after it."""
    m = _TRIM_LINE_RE.match(line)
    if m is None or _CONTRACT_RE.search(m.group("rest")) or not _PCT_RE.search(m.group("rest")):
        return None
    return m


class DragonAlertParser(Parser):
    name = "dragon_alert"

    def matches(self, message: ParsedMessage) -> bool:
        dragon = is_dragon_channel(message)
        if not (dragon or message.self_channel):
            return False
        return any(
            _trim_match(ln) or (dragon and (m := _ENTRY_RE.match(ln)) and _is_entry(m))
            for ln in _lines(message)
        )

    def parse(self, message: ParsedMessage) -> ParseResult:
        signals: list[TradeSignal] = []
        errors: list[str] = []
        # On the Self channel (the popup) only the trims are Dragon's: a typed
        # entry is still read by the parsers that always read it.
        dragon = is_dragon_channel(message)

        for line in _lines(message):
            m = _ENTRY_RE.match(line) if dragon else None
            if m and _is_entry(m):
                sig, err = self._entry(m, message)
                (signals.append(sig) if sig else errors.append(err))
                continue
            m = _trim_match(line)
            if m:
                sig, err = self._trim(m)
                (signals.append(sig) if sig else errors.append(err))

        if signals:
            return ParseResult.parsed_many(signals)
        if errors:
            return ParseResult.invalid(errors[0])
        return ParseResult.ignored("not a Dragon alert")

    def _entry(self, m: re.Match, message: ParsedMessage):
        strike = to_decimal(m.group("strike"))
        if strike is None or strike <= 0:
            return None, f"couldn't read the strike in {m.group(0).strip()!r}"
        price = to_decimal(m.group("price"))
        if price is None or price <= 0:
            return None, f"couldn't read the price in {m.group(0).strip()!r}"

        expiry, unspecified, err = _read_expiry(m.group("exp"), message)
        if err:
            return None, err

        return (
            TradeSignal(
                action=SignalAction.BUY,
                asset_type=AssetType.OPTION,
                symbol=m.group("symbol").upper(),
                option_type=_right(m.group("right")),
                strike=strike,
                expiration=expiry,
                expiry_unspecified=unspecified,
                quantity=DEFAULT_QUANTITY,
                order_type=OrderKind.LIMIT,
                limit_price=price,
                source_action="ENTRY",
                parser=self.name,
            ),
            None,
        )

    def _trim(self, m: re.Match):
        rest = m.group("rest")
        pct_m = _PCT_RE.search(rest)
        pct = to_decimal(pct_m.group("pct"))
        if pct is not None and (pct_m.group("sign") in ("-", "−")
                                or _DOWN_RE.search(rest[:pct_m.start()])):
            pct = -pct
        price_m = _TRIM_PRICE_RE.search(rest)
        price = to_decimal(price_m.group("price")) if price_m else None
        out = bool(_OUT_RE.search(rest))

        return (
            TradeSignal(
                action=SignalAction.SELL,
                asset_type=AssetType.OPTION,
                symbol=m.group("symbol").upper(),
                option_type=None,
                strike=None,
                expiration=None,
                # Only the ticker is written — the contract comes from what is
                # held in it: this channel's own latest contract first, else the
                # one open position in that ticker. Never a guess.
                expiry_unspecified=True,
                contract_unspecified=True,
                channel_contract_first=True,
                quantity=None,              # sized from the position held
                # Exits go to market — see compact_alert. The stated price is
                # kept for display.
                order_type=OrderKind.MARKET,
                limit_price=price,
                limit_price_unspecified=price is None,
                pnl_percent=pct,
                is_partial_close=not out,
                position_closed=out,
                flatten=out,
                source_action="CLOSE" if out else "TRIMMING",
                parser=self.name,
            ),
            None,
        )
