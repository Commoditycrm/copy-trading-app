"""The normalised trade signal, and the contract every parser implements.

A parser's job is to answer one of three things about a Discord message:

    IGNORED   this isn't a trade  (chatter, a join notice, a bot reply)
    INVALID   it looks like a trade but can't be read safely
    PARSED    here is exactly what it says

The middle case is the important one. A message that mentions a ticker but has
no strike, or an expiry we can't pin to a year, must NOT become a guessed trade —
it becomes INVALID with a reason a human can read. Nothing downstream is allowed
to infer the missing piece, because the cost of guessing wrong is a real
position in the wrong contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class AssetType(str, Enum):
    STOCK = "STOCK"
    OPTION = "OPTION"


class OptionType(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class OrderKind(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class ParseStatus(str, Enum):
    PARSED = "parsed"
    IGNORED = "ignored"
    INVALID = "invalid"


@dataclass
class TradeSignal:
    """A trade an alert is asking for, normalised across every source format."""

    action: SignalAction
    asset_type: AssetType
    symbol: str
    quantity: Decimal | None = None
    order_type: OrderKind = OrderKind.LIMIT
    limit_price: Decimal | None = None

    # Option-only. All three are required for an OPTION signal to be valid.
    option_type: OptionType | None = None
    strike: Decimal | None = None
    expiration: date | None = None

    # ── Everything else the card states ──────────────────────────────────
    # These are REPORTED figures from the alert, not computed by us. They make
    # the Discord tab show what the alert actually said rather than a lossy
    # summary of it, and they're what a reader needs to sanity-check a parse.
    #
    # Total dollar value of the fill, as stated ("1 @ $2.85 · $285"). NOT
    # recomputed from qty x price x 100 — if the source's arithmetic differs
    # from ours, the source's number is the one the trader saw.
    notional: Decimal | None = None
    # Realised P&L on THIS fill, for closes ("+$152 · +16%").
    pnl_amount: Decimal | None = None
    pnl_percent: Decimal | None = None
    # Position-level totals once fully closed ("total +$1,815 · +38%").
    total_pnl_amount: Decimal | None = None
    total_pnl_percent: Decimal | None = None
    # Size the position was opened at, from "2 of 5 still open".
    original_quantity: Decimal | None = None
    # True when the alert identified a contract but stated NO expiry — common on
    # exit alerts ("✂️ $SPY 769c +361%"), which assume you know what you hold.
    # The expiry must then be resolved from the OPEN POSITION at execution time,
    # never guessed here. Display shows the contract without a date.
    expiry_unspecified: bool = False
    # The card explicitly said the position is now flat.
    position_closed: bool = False

    # A SELL that closes only part of a position ("Sold 3 … 2 of 5 still open").
    # Treating a trim as a full exit would flatten a position the trader still
    # holds, so this is tracked explicitly rather than inferred from quantity.
    is_partial_close: bool = False
    remaining_quantity: Decimal | None = None

    # What the source actually called it (ENTERING / TRIMMING / CLOSING / BTO …).
    # Kept for display and debugging; never drives execution.
    source_action: str | None = None
    # Which parser produced this, so a bad reading is traceable to its author.
    parser: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form for storage and the API."""
        return {
            "action": self.action.value,
            "asset_type": self.asset_type.value,
            "symbol": self.symbol,
            "quantity": str(self.quantity) if self.quantity is not None else None,
            "order_type": self.order_type.value,
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "option_type": self.option_type.value if self.option_type else None,
            "strike": str(self.strike) if self.strike is not None else None,
            "expiration": self.expiration.isoformat() if self.expiration else None,
            "is_partial_close": self.is_partial_close,
            "remaining_quantity": (
                str(self.remaining_quantity) if self.remaining_quantity is not None else None
            ),
            "notional": str(self.notional) if self.notional is not None else None,
            "pnl_amount": str(self.pnl_amount) if self.pnl_amount is not None else None,
            "pnl_percent": str(self.pnl_percent) if self.pnl_percent is not None else None,
            "total_pnl_amount": (
                str(self.total_pnl_amount) if self.total_pnl_amount is not None else None
            ),
            "total_pnl_percent": (
                str(self.total_pnl_percent) if self.total_pnl_percent is not None else None
            ),
            "original_quantity": (
                str(self.original_quantity) if self.original_quantity is not None else None
            ),
            "position_closed": self.position_closed,
            "expiry_unspecified": self.expiry_unspecified,
            "source_action": self.source_action,
            "parser": self.parser,
        }


@dataclass
class ParseResult:
    """What a parser concluded, and why.

    ``signals`` is a LIST because one message can carry several trades — alert
    channels routinely post a block of exits in one message:

        ✂️ $SPY 769c +361%
        ✂️ $SPY 770c +372%

    Reading only the first would silently drop the rest, so every signal is
    returned and the caller decides how to present them.
    """

    status: ParseStatus
    signals: list[TradeSignal] = field(default_factory=list)
    # Human-readable, shown in the UI. Required for IGNORED/INVALID — "why
    # didn't this alert trade?" has to be answerable without reading code.
    reason: str | None = None

    @property
    def signal(self) -> TradeSignal | None:
        """The first signal, for the common single-trade case."""
        return self.signals[0] if self.signals else None

    @classmethod
    def parsed(cls, signal: TradeSignal) -> "ParseResult":
        return cls(status=ParseStatus.PARSED, signals=[signal])

    @classmethod
    def parsed_many(cls, signals: list[TradeSignal]) -> "ParseResult":
        return cls(status=ParseStatus.PARSED, signals=list(signals))

    @classmethod
    def ignored(cls, reason: str) -> "ParseResult":
        return cls(status=ParseStatus.IGNORED, reason=reason)

    @classmethod
    def invalid(cls, reason: str) -> "ParseResult":
        return cls(status=ParseStatus.INVALID, reason=reason)


@dataclass
class ParsedMessage:
    """A Discord message flattened into the fields a parser reads.

    Alert bots typically put everything in an embed and leave ``content`` empty,
    so ``text`` is the two concatenated — a parser that only looked at content
    would reject the entire real-world feed.
    """

    content: str = ""
    embeds: list[dict[str, Any]] = field(default_factory=list)
    author: str | None = None
    posted_at: datetime | None = None

    @property
    def text(self) -> str:
        """Everything readable, newline-joined: content, then each embed's
        title / description / fields / footer."""
        parts: list[str] = []
        if self.content.strip():
            parts.append(self.content.strip())
        for e in self.embeds or []:
            for key in ("title", "description"):
                v = (e.get(key) or "").strip()
                if v:
                    parts.append(v)
            for f in e.get("fields") or []:
                name, value = (f.get("name") or "").strip(), (f.get("value") or "").strip()
                if name or value:
                    parts.append(f"{name} {value}".strip())
        return "\n".join(parts)


class Parser(Protocol):
    """One alert format.

    Different Discord channels format alerts completely differently, so parsing
    is a registry of small parsers rather than one function with a growing pile
    of branches. Each declares whether it recognises a message, and only then is
    asked to read it.
    """

    name: str

    def matches(self, message: ParsedMessage) -> bool:
        """Cheap check: is this message in this parser's format at all?"""
        ...

    def parse(self, message: ParsedMessage) -> ParseResult:
        """Read a message this parser has already claimed."""
        ...
