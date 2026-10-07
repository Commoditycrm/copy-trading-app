"""Parser for terse, chatty alerts — the BREAKDOWNSNIPER house style.

Observed live (2026-09-30), mentions and "(edited)" included:

    AMZN245P @here @Sniper .55                     entry: AMZN 245 put, no expiry
    Adding .4 @here @Sniper                        add to the position, no contract
    Holy moly what an add trim @here @Sniper .63 moving crazy AMZN 25%   a trim

Every other parser ignores these: the contract is glued together
("AMZN245P"), the price is a bare ".55", adds name nothing, and trims are
prose. They are read here — LAST in the chain, so a message any other parser
understands is never seen by this one.

What each carries, and what execution fills in:

  * entry  — BUY, one contract, limit at the stated price. No expiry: flagged
    ``nearest_expiry``, so execution buys the nearest listed expiry.
  * add    — BUY at the stated price, ``add_to_latest`` + ``double_up``:
    execution resolves the contract from this CHANNEL's latest open position
    and doubles it (average down). With nothing open — stopped out — it
    re-enters the channel's latest contract as a new position.
  * trim   — SELL of the named symbol's position through the exit ladder.
    "add trim" is a trim: a stated gain is an exit call, not a buy.

Also live (2026-10-06), missed at the time:

    QQQ 759P @here @everyone out the gate high risk     entry: spaced, no price

A SPACED contract is commentary unless the message opens with "In"/"Entry" —
or pings the channel (@here / @everyone), which is how this author marks a
call. Pinged, it is an entry; with no price ("out the gate") the price comes
from the live quote at execution (a marketable limit at the ask). A pinged
message that reads as an exit (a %, "sold", "trim", "out of" …) is never an
entry.

The rule throughout is the same as every parser here: refuse rather than
guess. A message that fits none of the three shapes exactly is ignored.
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

# Discord mentions and the client's "(edited)" marker are noise, not content.
# Never "@0.80" or "@$1.10": that's a price, not a mention.
_MENTION_RE = re.compile(r"@(?![\d.$])\S+")
_EDITED_RE = re.compile(r"\(edited\)", re.IGNORECASE)

# "AMZN245P", "$SPY765.5C" — ticker, strike and right with no spaces.
_GLUED_RE = re.compile(r"(?<![A-Za-z0-9])\$?(?P<sym>[A-Z]{1,5})(?P<strike>\d{1,5}(?:\.\d+)?)(?P<right>[CP])(?![A-Za-z0-9])")
# "In SPY 763P 1.01" — ticker and contract SPACED, which only counts as an
# entry when the message opens with an entry word: spaced, the same text is
# also how commentary names a contract ("SPY 763P hit 1.50").
_SPACED_ENTRY_RE = re.compile(
    # BTO / Buy / Bought / Long / Entered are the free-text parser's: it runs
    # first and requires an explicit expiry. "In" and "Entry" reach here.
    r"^\s*(?:in|entry)\b[\s:]+"
    r"(?-i:\$?(?P<sym>[A-Z]{1,5}))\s+(?P<strike>\d{1,5}(?:\.\d+)?)(?P<right>[CcPp])(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# "QQQ 759P" anywhere in the message — an entry only when the channel is pinged.
_SPACED_ANY_RE = re.compile(
    r"(?<![A-Za-z0-9$])\$?(?P<sym>[A-Z]{1,5})\s+(?P<strike>\d{1,5}(?:\.\d+)?)(?P<right>[CcPp])(?![A-Za-z0-9])"
)
# The author pinging the channel: what marks a call rather than commentary.
_PING_RE = re.compile(r"@(?:here|everyone)\b", re.IGNORECASE)
# A pinged message that is about getting OUT, not in: never an entry.
_EXIT_TALK_RE = re.compile(
    r"%|\b(?:sold|sell(?:ing)?|clos(?:e|ed|ing)|exit(?:ed|ing)?|trim\w*|cut|stopped|"
    r"took\s+profits?|profits?\s+taken|out\s+of)\b",
    re.IGNORECASE,
)
# A bare option price: ".55", "0.55", "1.2", "@.63". Not a percentage.
_PRICE_RE = re.compile(r"(?<![\w.%])@?\s*\$?(?P<price>\d*\.\d+)(?!\s*%)(?![\w.])")
# "Adding .4" / "add 0.40" at the START — an add that names no contract.
_ADD_RE = re.compile(r"^\s*add(?:ing)?\s+@?\s*\$?(?P<price>\d*\.\d+|\d+(?:\.\d+)?)(?![\w.%])",
                     re.IGNORECASE)
_TRIM_RE = re.compile(r"\btrim(?:med|ming|s)?\b", re.IGNORECASE)
# "Average down on SPY @.80" / "averaging down $SPY 0.80" / "avg down SPY .8":
# double the ONE open position in that symbol. The price is required.
# "Stopped out of rest of SPY calls" / "stopped out on $SPY puts" / "Stopped
# out SPY": the author is fully out — close everything matching from this
# channel. The ticker must be written as one (capitals or $).
_STOPPED_RE = re.compile(r"\bstopped\s+out\b", re.IGNORECASE)
# "Cutting @here @Sniper 10% loss" / "cut IWM": the author is out of the trade —
# a full close, like a stop-out. The % is the result, not an instruction.
_CUT_RE = re.compile(r"\bcut(?:ting|s)?\b", re.IGNORECASE)
_STOP_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$?(?P<sym>[A-Z]{1,5})(?![A-Za-z0-9])")
_RIGHT_WORD_RE = re.compile(r"\b(?P<right>calls?|puts?)\b", re.IGNORECASE)
_AVG_DOWN_RE = re.compile(
    # The ticker as tickers are written — capitals or $ — so "I might average
    # down later" is chatter, not an order for LATER.
    r"\b(?:average|averaging|avg)\s+down\s+(?:on\s+|in\s+)?(?-i:\$?(?P<sym>[A-Z]{1,5}))\b",
    re.IGNORECASE,
)
_PCT_RE = re.compile(r"(?P<sign>[+\-−])?\s*(?P<pct>\d+(?:\.\d+)?)\s*%")
# A bare ticker in a trim ("... AMZN 25%"): an all-caps word of 2-5 letters.
_TICKER_RE = re.compile(r"(?<![A-Za-z0-9$])\$?(?P<sym>[A-Z]{2,5})(?![A-Za-z0-9])")
# All-caps words that are not tickers in these messages.
_NOT_TICKERS = {"ALL", "OUT", "BTO", "STC", "ATH", "EOD", "DTE", "ITM", "OTM", "ATM", "LOL", "OMG", "PT"}

_ENTRY_QTY = Decimal(1)


def _clean(text: str) -> str:
    return " ".join(_EDITED_RE.sub(" ", _MENTION_RE.sub(" ", text or "")).split())


def _pinged_entry(message: ParsedMessage, text: str) -> "re.Match | None":
    """A spaced contract in a message that pings the channel and isn't exit talk."""
    if not _PING_RE.search(message.content or "") or _EXIT_TALK_RE.search(text):
        return None
    return _SPACED_ANY_RE.search(text)


class TerseAlertParser(Parser):
    name = "terse_alert"

    def matches(self, message: ParsedMessage) -> bool:
        text = _clean(message.content)
        return bool(text) and bool(
            _TRIM_RE.search(text) or _ADD_RE.match(text) or _GLUED_RE.search(text)
            or _AVG_DOWN_RE.search(text) or _STOPPED_RE.search(text) or _CUT_RE.search(text)
            or _SPACED_ENTRY_RE.match(text) or _pinged_entry(message, text)
        )

    def parse(self, message: ParsedMessage) -> ParseResult:
        text = _clean(message.content)

        # Trim first: "what an add trim ... AMZN 25%" is an exit, whatever
        # else the sentence says.
        if _STOPPED_RE.search(text):
            return self._stopped_out(text)
        if _CUT_RE.search(text):
            return self._stopped_out(text, source_action="CUTTING")

        if _TRIM_RE.search(text):
            return self._trim(text)

        avg = _AVG_DOWN_RE.search(text)
        if avg and avg.group("sym").upper() not in _NOT_TICKERS:
            return self._average_down(text, avg)

        add = _ADD_RE.match(text)
        if add and not _GLUED_RE.search(text):
            price = to_decimal(add.group("price"))
            if price is None or price <= 0:
                return ParseResult.ignored("an add with no usable price")
            return ParseResult.parsed(TradeSignal(
                action=SignalAction.BUY,
                asset_type=AssetType.OPTION,
                symbol=None,
                quantity=None,              # sized from the position (double_up)
                order_type=OrderKind.LIMIT,
                limit_price=price,
                double_up=True,
                add_to_latest=True,
                contract_unspecified=True,
                source_action="ADDING",
                parser=self.name,
            ))

        pinged = bool(_PING_RE.search(message.content or "")) and not _EXIT_TALK_RE.search(text)
        glued = (_GLUED_RE.search(text) or _SPACED_ENTRY_RE.match(text)
                 or _pinged_entry(message, text))
        if glued:
            rest = text[:glued.start()] + " " + text[glued.end():]
            price_m = _PRICE_RE.search(rest)
            price = to_decimal(price_m.group("price")) if price_m else None
            if price_m and (price is None or price <= 0):
                return ParseResult.ignored("a contract with no usable price")
            if price is None and not pinged:
                return ParseResult.ignored("a contract with no price")
            # Pinged with no price ("out the gate"): buy now — execution prices
            # it from the live quote, a limit at the ask.
            return ParseResult.parsed(TradeSignal(
                action=SignalAction.BUY,
                asset_type=AssetType.OPTION,
                symbol=glued.group("sym"),
                option_type=OptionType.CALL if glued.group("right").upper() == "C" else OptionType.PUT,
                strike=to_decimal(glued.group("strike")),
                quantity=_ENTRY_QTY,
                order_type=OrderKind.LIMIT,
                limit_price=price,
                limit_price_unspecified=price is None,
                expiry_unspecified=True,
                nearest_expiry=True,
                source_action="ENTRY",
                parser=self.name,
            ))

        return ParseResult.ignored("not a terse alert")

    def _stopped_out(self, text: str, source_action: str = "STOPPED_OUT") -> ParseResult:
        """The author is out — stopped out, or cutting the trade: close every
        matching position this channel opened, at market. Execution finds them;
        none held = refused. Naming no ticker means the position the channel is
        in (``latest_contract``), as "Adding .4" does."""
        glued = _GLUED_RE.search(text)
        tickers = {m.group("sym") for m in _STOP_TICKER_RE.finditer(text)
                   if m.group("sym") not in _NOT_TICKERS}
        if glued:
            tickers = {glued.group("sym")}
        if len(tickers) > 1:
            return ParseResult.invalid(
                "a close-out that names more than one ticker — no way to tell which to close"
            )
        if not tickers:
            return ParseResult.parsed(TradeSignal(
                action=SignalAction.SELL,
                asset_type=AssetType.OPTION,
                symbol=None,
                quantity=None,              # everything held
                order_type=OrderKind.MARKET,
                limit_price_unspecified=True,
                position_closed=True,
                flatten=True,
                close_all_matching=True,
                latest_contract=True,       # the position this channel is in
                expiry_unspecified=True,
                contract_unspecified=True,
                source_action=source_action,
                parser=self.name,
            ))
        right_m = _RIGHT_WORD_RE.search(text)
        right = (glued.group("right") if glued else
                 (right_m.group("right")[0].upper() if right_m else None))
        return ParseResult.parsed(TradeSignal(
            action=SignalAction.SELL,
            asset_type=AssetType.OPTION,
            symbol=tickers.pop(),
            option_type=(None if right is None else
                         OptionType.CALL if right == "C" else OptionType.PUT),
            strike=to_decimal(glued.group("strike")) if glued else None,
            quantity=None,                  # everything held
            order_type=OrderKind.MARKET,
            limit_price_unspecified=True,
            position_closed=True,
            flatten=True,
            close_all_matching=True,
            expiry_unspecified=True,
            contract_unspecified=True,
            source_action=source_action,
            parser=self.name,
        ))

    def _average_down(self, text: str, m: re.Match) -> ParseResult:
        """Double the one open position in the named symbol, at the stated price.

        Execution fills the contract from the position held (refusing when there
        is none, or more than one) and sizes it as the holding (double_up). With
        no price there is nothing safe to bid, so it is refused, not guessed.
        """
        rest = text[:m.start()] + " " + text[m.end():]
        glued = _GLUED_RE.search(rest)
        price_m = _PRICE_RE.search(rest[:glued.start()] + " " + rest[glued.end():] if glued else rest)
        price = to_decimal(price_m.group("price")) if price_m else None
        if price is None or price <= 0:
            return ParseResult.invalid("an average-down with no price — add one, e.g. @0.80")
        return ParseResult.parsed(TradeSignal(
            action=SignalAction.BUY,
            asset_type=AssetType.OPTION,
            symbol=m.group("sym").upper(),
            option_type=(None if not glued else
                         OptionType.CALL if glued.group("right") == "C" else OptionType.PUT),
            strike=to_decimal(glued.group("strike")) if glued else None,
            quantity=None,              # sized from the position (double_up)
            order_type=OrderKind.LIMIT,
            limit_price=price,
            double_up=True,
            expiry_unspecified=True,
            contract_unspecified=glued is None,
            source_action="AVERAGE_DOWN",
            parser=self.name,
        ))

    def _trim(self, text: str) -> ParseResult:
        glued = _GLUED_RE.search(text)
        if glued:
            symbol, strike = glued.group("sym"), to_decimal(glued.group("strike"))
            option_type = OptionType.CALL if glued.group("right") == "C" else OptionType.PUT
        else:
            tickers = [m.group("sym") for m in _TICKER_RE.finditer(text)
                       if m.group("sym") not in _NOT_TICKERS]
            if len(set(tickers)) != 1:
                # None, or several: no way to tell which position is meant.
                return ParseResult.ignored("a trim that doesn't name exactly one ticker")
            symbol, strike, option_type = tickers[0], None, None

        price_m = _PRICE_RE.search(text)
        signal = TradeSignal(
            action=SignalAction.SELL,
            asset_type=AssetType.OPTION,
            symbol=symbol,
            option_type=option_type,
            strike=strike,
            quantity=None,                  # sized from the position held
            # Exits go to market — see compact_alert.
            order_type=OrderKind.MARKET,
            limit_price=to_decimal(price_m.group("price")) if price_m else None,
            limit_price_unspecified=price_m is None,
            is_partial_close=True,
            expiry_unspecified=True,
            contract_unspecified=strike is None,
            source_action="TRIMMING",
            parser=self.name,
        )
        pct = _PCT_RE.search(text)
        if pct:
            value = to_decimal(pct.group("pct"))
            if value is not None:
                signal.pnl_percent = -value if (pct.group("sign") or "") in "-−" and pct.group("sign") else value
        return ParseResult.parsed(signal)
