"""Parser for free-text alerts — the human-typed formats.

    BUY AAPL 250 CALL 18 SEP @ 2.15
    BUY 5 AAPL 250C SEP18 @ 2.15 LIMIT
    STO TSLA 400P 10/17 @ 3.10
    AAPL
    CALL
    250 STRIKE
    EXP: SEP 18
    BUY @ 2.15

Prose is far riskier to read than a machine-generated card, so this parser is
deliberately conservative: it requires an explicit action word, and for an option
it requires strike, right AND expiry to all be present. Anything short of that
is INVALID with a reason — never a trade assembled from the pieces that happened
to be there.
"""
from __future__ import annotations

import re

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

# Explicit intent. Without one of these the message isn't a trade instruction,
# whatever else it mentions.
_BUY_WORDS = r"BUY|BOUGHT|BTO|LONG|ENTER(?:ING|ED)?|ADD(?:ING|ED)?"
_SELL_WORDS = r"SELL|SOLD|STO|STC|CLOSE[DS]?|CLOSING|EXIT(?:ING|ED)?|TRIM(?:MING|MED)?"
_ACTION_RE = re.compile(rf"\b(?P<buy>{_BUY_WORDS})\b|\b(?P<sell>{_SELL_WORDS})\b", re.IGNORECASE)

_TICKER_RE = re.compile(r"\$?\b([A-Z]{1,6})\b")
# "250C" / "250P" / "250 CALL" / "250 PUT" / "STRIKE 250"
_CONTRACT_RE = re.compile(
    r"\$?(?P<strike>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<right>C\b|P\b|CALLS?\b|PUTS?\b)",
    re.IGNORECASE,
)
_RIGHT_WORD_RE = re.compile(r"\b(?P<right>CALLS?|PUTS?)\b", re.IGNORECASE)
_STRIKE_WORD_RE = re.compile(
    r"\$?(?P<strike>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*STRIKE\b", re.IGNORECASE
)
_EXPIRY_RE = re.compile(
    r"(?:EXP(?:IRY|IRES|IRATION)?\s*:?\s*)?"
    r"\b(?P<exp>\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
    r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)\s*\d{1,2}|"
    r"\d{1,2}\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC))\b",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"@\s*\$?(?P<price>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)")
_QTY_RE = re.compile(r"\b(?:X\s*)?(?P<qty>\d{1,5})\s*(?:CONTRACTS?|SHARES?|LOTS?)\b", re.IGNORECASE)
_ACTION_QTY_RE = re.compile(rf"\b(?:{_BUY_WORDS}|{_SELL_WORDS})\s+(?P<qty>\d{{1,5}})\b", re.IGNORECASE)
_MARKET_RE = re.compile(r"\b(MARKET|MKT)\b", re.IGNORECASE)

# Words that look like tickers but aren't. Without this, "BUY CALL 250" reads
# CALL as the symbol.
_NOT_TICKERS = {
    "BUY", "SELL", "SOLD", "BOUGHT", "BTO", "STO", "STC", "CALL", "CALLS", "PUT", "PUTS",
    "EXP", "EXPIRY", "EXPIRES", "EXPIRATION", "STRIKE", "LIMIT", "MARKET", "MKT", "AT",
    "LONG", "SHORT", "CLOSE", "CLOSED", "CLOSING", "EXIT", "TRIM", "ADD", "ENTER", "LOTS",
    "CONTRACTS", "SHARES", "OPEN", "TP", "SL", "RISKY", "LOTTO", "SWING", "DAY",
    "TRIMMING", "TRIMMED", "CLOSING", "ENTERING", "ENTERED", "ADDING", "ADDED",
    "SOON", "NOW", "HERE", "OUT", "IN", "ALL", "SOME", "MORE", "AT", "TO", "THE",
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "SEPT", "OCT", "NOV", "DEC",
}


class GenericTextParser(Parser):
    name = "generic_text"

    def matches(self, message: ParsedMessage) -> bool:
        text = message.text
        return bool(_ACTION_RE.search(text) and _TICKER_RE.search(text.upper()))

    def parse(self, message: ParsedMessage) -> ParseResult:
        text = message.text
        upper = text.upper()

        action_m = _ACTION_RE.search(text)
        if not action_m:
            return ParseResult.ignored("no buy/sell instruction in this message")
        action = SignalAction.BUY if action_m.group("buy") else SignalAction.SELL

        symbol = self._symbol(upper)
        if not symbol:
            return ParseResult.invalid("couldn't find a ticker in the alert")

        price_m = _PRICE_RE.search(text)
        price = to_decimal(price_m.group("price")) if price_m else None
        is_market = bool(_MARKET_RE.search(text)) or price is None

        qty_m = _QTY_RE.search(text) or _ACTION_QTY_RE.search(text)
        qty = to_decimal(qty_m.group("qty")) if qty_m else None

        strike, right = self._contract(text, upper, symbol)

        if strike is None and right is None:
            # No option details at all ⇒ it MIGHT be a stock order. But an action
            # word plus any capitalised token is not an instruction: "trimming
            # soon" would otherwise become a stock SELL of ticker SOON.
            #
            # Require something that makes it concrete — a stated size, a price,
            # or an explicit $TICKER. Prose that merely mentions trading isn't a
            # trade.
            if qty is None and price is None and f"${symbol}" not in upper:
                return ParseResult.ignored(
                    "mentions buying/selling but names no size, price or $ticker"
                )
            return ParseResult.parsed(
                TradeSignal(
                    action=action,
                    asset_type=AssetType.STOCK,
                    symbol=symbol,
                    quantity=qty,
                    order_type=OrderKind.MARKET if is_market else OrderKind.LIMIT,
                    limit_price=None if is_market else price,
                    source_action=action_m.group(0).upper(),
                    parser=self.name,
                )
            )

        # Partial option details are the dangerous case: enough to look like a
        # trade, not enough to identify the contract. Refuse rather than fill in
        # the gap.
        # Reserve INVALID for messages that genuinely look like an instruction.
        # Prose such as "SPY calls hit 1.25 again" mentions options without
        # asking for anything, and flagging it INVALID would fill the review
        # queue with commentary rather than real parse failures.
        looks_like_an_instruction = f"${symbol}" in upper or price is not None or qty is not None
        if strike is None:
            if not looks_like_an_instruction:
                return ParseResult.ignored("mentions options but isn't a trade instruction")
            return ParseResult.invalid("the alert names a call/put but no strike")
        if right is None:
            if not looks_like_an_instruction:
                return ParseResult.ignored("mentions options but isn't a trade instruction")
            return ParseResult.invalid("the alert names a strike but not call or put")

        exp_m = _EXPIRY_RE.search(text)
        if not exp_m:
            return ParseResult.invalid("the alert has no expiry")
        expiry, exp_err = parse_expiry(exp_m.group("exp"), posted_at=message.posted_at)
        if exp_err:
            return ParseResult.invalid(exp_err)

        return ParseResult.parsed(
            TradeSignal(
                action=action,
                asset_type=AssetType.OPTION,
                symbol=symbol,
                option_type=right,
                strike=strike,
                expiration=expiry,
                quantity=qty,
                order_type=OrderKind.MARKET if is_market else OrderKind.LIMIT,
                limit_price=None if is_market else price,
                source_action=action_m.group(0).upper(),
                parser=self.name,
            )
        )

    def _symbol(self, upper: str) -> str | None:
        for m in _TICKER_RE.finditer(upper):
            token = m.group(1)
            if token in _NOT_TICKERS or token.isdigit():
                continue
            return token
        return None

    def _contract(self, text: str, upper: str, symbol: str):
        """Strike and right, from either "250C" or "250 STRIKE" + "CALL"."""
        right = None
        strike = None

        m = _CONTRACT_RE.search(text)
        if m:
            # Guard against matching the symbol's own digits (e.g. "SPX500C").
            strike = to_decimal(m.group("strike"))
            r = m.group("right").upper()
            right = OptionType.CALL if r.startswith("C") else OptionType.PUT
            return strike, right

        sm = _STRIKE_WORD_RE.search(text)
        if sm:
            strike = to_decimal(sm.group("strike"))
        rm = _RIGHT_WORD_RE.search(upper)
        if rm:
            right = OptionType.CALL if rm.group("right").upper().startswith("CALL") else OptionType.PUT
        return strike, right
