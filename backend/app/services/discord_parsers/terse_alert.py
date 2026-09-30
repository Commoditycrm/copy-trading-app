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
    and doubles it (average down), refusing if the channel has nothing open.
  * trim   — SELL of the named symbol's position through the exit ladder.
    "add trim" is a trim: a stated gain is an exit call, not a buy.

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
_MENTION_RE = re.compile(r"@\S+")
_EDITED_RE = re.compile(r"\(edited\)", re.IGNORECASE)

# "AMZN245P", "$SPY765.5C" — ticker, strike and right with no spaces.
_GLUED_RE = re.compile(r"(?<![A-Za-z0-9])\$?(?P<sym>[A-Z]{1,5})(?P<strike>\d{1,5}(?:\.\d+)?)(?P<right>[CP])(?![A-Za-z0-9])")
# A bare option price: ".55", "0.55", "1.2", "@.63". Not a percentage.
_PRICE_RE = re.compile(r"(?<![\w.%])@?\s*\$?(?P<price>\d*\.\d+)(?!\s*%)(?![\w.])")
# "Adding .4" / "add 0.40" at the START — an add that names no contract.
_ADD_RE = re.compile(r"^\s*add(?:ing)?\s+@?\s*\$?(?P<price>\d*\.\d+|\d+(?:\.\d+)?)(?![\w.%])",
                     re.IGNORECASE)
_TRIM_RE = re.compile(r"\btrim(?:med|ming|s)?\b", re.IGNORECASE)
_PCT_RE = re.compile(r"(?P<sign>[+\-−])?\s*(?P<pct>\d+(?:\.\d+)?)\s*%")
# A bare ticker in a trim ("... AMZN 25%"): an all-caps word of 2-5 letters.
_TICKER_RE = re.compile(r"(?<![A-Za-z0-9$])\$?(?P<sym>[A-Z]{2,5})(?![A-Za-z0-9])")
# All-caps words that are not tickers in these messages.
_NOT_TICKERS = {"ALL", "OUT", "BTO", "STC", "ATH", "EOD", "DTE", "ITM", "OTM", "ATM", "LOL", "OMG", "PT"}

_ENTRY_QTY = Decimal(1)


def _clean(text: str) -> str:
    return " ".join(_EDITED_RE.sub(" ", _MENTION_RE.sub(" ", text or "")).split())


class TerseAlertParser(Parser):
    name = "terse_alert"

    def matches(self, message: ParsedMessage) -> bool:
        text = _clean(message.content)
        return bool(text) and bool(
            _TRIM_RE.search(text) or _ADD_RE.match(text) or _GLUED_RE.search(text)
        )

    def parse(self, message: ParsedMessage) -> ParseResult:
        text = _clean(message.content)

        # Trim first: "what an add trim ... AMZN 25%" is an exit, whatever
        # else the sentence says.
        if _TRIM_RE.search(text):
            return self._trim(text)

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

        glued = _GLUED_RE.search(text)
        if glued:
            rest = text[:glued.start()] + " " + text[glued.end():]
            price_m = _PRICE_RE.search(rest)
            if not price_m:
                return ParseResult.ignored("a contract with no price")
            price = to_decimal(price_m.group("price"))
            if price is None or price <= 0:
                return ParseResult.ignored("a contract with no usable price")
            return ParseResult.parsed(TradeSignal(
                action=SignalAction.BUY,
                asset_type=AssetType.OPTION,
                symbol=glued.group("sym"),
                option_type=OptionType.CALL if glued.group("right") == "C" else OptionType.PUT,
                strike=to_decimal(glued.group("strike")),
                quantity=_ENTRY_QTY,
                order_type=OrderKind.LIMIT,
                limit_price=price,
                expiry_unspecified=True,
                nearest_expiry=True,
                source_action="ENTRY",
                parser=self.name,
            ))

        return ParseResult.ignored("not a terse alert")

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
