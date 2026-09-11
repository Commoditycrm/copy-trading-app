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
        return result

    if first_ignored is not None:
        return first_ignored
    return ParseResult.ignored("not a trade alert")


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
