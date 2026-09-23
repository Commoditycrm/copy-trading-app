"""Discord alert parsing — turning a message into a normalised trade signal.

    Parser
     ├── AlertCardParser    structured ENTERING/TRIMMING/CLOSING embeds
     ├── CompactAlertParser terse one-liners ("$TSLA 375 CALL 0DTE @0.95",
     │                      "✂️ $SPY 769c +361%") — no action word
     └── GenericTextParser  free-text ("BUY AAPL 250C SEP18 @ 2.15")

A registry rather than one function, because different channels format alerts
completely differently and hardcoding one channel's shape into the pipeline
would make every other source a rewrite. Adding a provider means adding a module
and one line here.

Order matters: the most specific format is tried first. The structured card is
machine-generated and unambiguous, so it should claim its own messages before
the free-text parser gets a chance to half-read them.

Nothing here executes anything. A parsed signal is a reading of what a message
SAYS — validation, risk checks and execution are separate stages, deliberately.
"""
from __future__ import annotations

import logging
import re

from .alert_card import AlertCardParser
from .compact_alert import CompactAlertParser
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
from .generic_text import GenericTextParser

log = logging.getLogger(__name__)

# An author marking an entry as a smaller one. Matched on the whole message
# rather than inside a parser: it is the author's turn of phrase, not a property
# of any channel's format, so every parser's output gets the same treatment.
#
# Word-bounded on purpose. A bare "light" must not fire on "lighten", which is
# the opposite instruction (trim a position), nor on "delight" or "flashlight".
# "not heavy" tolerates a hyphen or extra spaces because people type both.
_HALF_SIZE_RE = re.compile(r"\bnot[\s-]+heavy\b|\blight\b", re.IGNORECASE)


def _is_half_size(text: str) -> bool:
    return bool(_HALF_SIZE_RE.search(text or ""))

# Most specific first.
PARSERS: list[Parser] = [
    AlertCardParser(),
    # Before the free-text parser: "$TSLA 375 CALL 0DTE @0.95" has no action
    # word, so the generic parser would ignore it entirely.
    CompactAlertParser(),
    GenericTextParser(),
]

_BY_NAME = {p.name: p for p in PARSERS}


def parse_message(message: ParsedMessage, *, parser_key: str | None = None) -> ParseResult:
    """Read one message. Never raises.

    ``parser_key`` pins a source to one format when its channel is known; the
    default tries each in turn. A parser that throws is treated as "didn't
    match" and the next one is tried — a bug in one format must not stop a
    different channel's alerts from being read.
    """
    if parser_key:
        parser = _BY_NAME.get(parser_key)
        if parser is None:
            return ParseResult.invalid(f"unknown parser {parser_key!r}")
        candidates = [parser]
    else:
        candidates = PARSERS

    # The first specific "this isn't a trade, and here's why" we're told. A
    # later parser's generic verdict must not overwrite it: "price update on an
    # open position" is genuinely useful in the audit trail, "not a trade alert"
    # is not.
    first_ignored: ParseResult | None = None

    for parser in candidates:
        try:
            if not parser.matches(message):
                continue
            result = parser.parse(message)
        except Exception:  # noqa: BLE001
            log.exception("discord parser %s raised; trying the next", parser.name)
            continue
        # A parser that claimed the message but found it isn't a trade shouldn't
        # block a later parser from reading it — keep looking, but remember why.
        if result.status is ParseStatus.IGNORED:
            if first_ignored is None and result.reason:
                first_ignored = result
            continue
        _mark_half_size(message, result)
        return result

    if first_ignored is not None:
        return first_ignored
    return ParseResult.ignored("not a trade alert")


def _mark_half_size(message: ParsedMessage, result: ParseResult) -> None:
    """Flag BUY signals the author called "light" or "not heavy".

    Entries only. On a SELL the same words mean something different — "lighten
    up" is an instruction to trim — and an exit is sized from the position held,
    never from the alert, so halving one would strand part of a position.
    """
    if not _is_half_size(message.text):
        return
    for signal in result.signals or []:
        if signal.action is SignalAction.BUY:
            signal.half_size = True


__all__ = [
    "PARSERS",
    "AssetType",
    "OptionType",
    "OrderKind",
    "ParseResult",
    "ParseStatus",
    "ParsedMessage",
    "Parser",
    "SignalAction",
    "TradeSignal",
    "parse_message",
]
