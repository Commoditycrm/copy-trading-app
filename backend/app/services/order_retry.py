"""Broker-call recovery wrapper around BrokerAdapter.place_order().

Why this exists
---------------
Jayesh's signalboxx codebase had explicit recovery logic for common broker
rejections (market-order-after-hours, 0DTE-expired, rate-limit-throttle). In
this app, before this module, a rejection bubbled straight back to the user
as an audit entry + red status pill. Most rejections fall into a small set
of well-known categories — we can either retry them automatically or surface
a much clearer message than "broker_error: 422 {...}".

Scope
-----
1. **Transient errors** (HTTP 5xx, 429 rate limit, connection reset, request
   timeout) — auto-retry with exponential backoff. Up to `max_attempts`.
2. **User-fixable errors** (after-hours market order, expired option, asset
   not tradable, insufficient buying power) — normalized into a short, plain
   message we attach to the order's reject_reason. No retry.
3. **Unknown errors** — re-raise with the original message. Caller handles.

Out of scope (yet)
------------------
- Auto-converting market → limit with the live quote when the broker rejects
  for after-hours. That requires `get_latest_quote()` on the adapter and is
  a follow-up. For now we just label the rejection clearly so the trader
  knows to retry as a limit.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from app.brokers.base import BrokerAdapter, BrokerOrderRequest, BrokerOrderResult

log = logging.getLogger(__name__)


def is_order_conflict_error(exc: Exception) -> bool:
    """True when a broker rejected an order because ANOTHER working order for the
    same contract is blocking it — resolvable by cancelling that order and
    retrying. Covers both directions:
      * OPPOSITE side → wash trade ('wash trade' / 'opposite side ... order
        exists' / 'cannot open a short sell while a long buy order is open');
      * SAME side → the existing order already reserves the position, so a second
        close is 'uncovered' (options) or 'insufficient qty available' (stocks).

    Matches on the specific MESSAGE — NOT the bare Alpaca code 40310000, which is
    a broad family that also covers non-cancellable rejections like 'asset X is
    not fractionable' / 'not tradable'. Matching the code would wrongly cancel
    the user's working orders for those. Shared by the direct-close path
    (api.trades) and the copy-engine mirror-close path.

    Covers BOTH broker vocabularies:
      * Alpaca (direct) — 'wash trade' / 'opposite side' / 'insufficient qty' /
        'held_for_orders';
      * Webull via SnapTrade — 'cancel the existing order first to place the new
        order' and its variants.
    A false positive is harmless: the caller looks for a same-contract working
    order to cancel and, finding none, re-raises the original error unchanged."""
    m = str(exc).lower()
    return (
        "wash trade" in m
        or "opposite side" in m
        or "opposite-side" in m
        or "long buy order is open" in m
        or "short sell order is open" in m
        or "uncovered" in m
        or "insufficient qty" in m
        or "insufficient quantity" in m
        or "held_for_orders" in m
        # Webull / SnapTrade "there's already a working order — cancel it first".
        # Real message (code 1119): "You can not place order in excess of current
        # holding quantity to create a position on the other side of the market.
        # Please check your open orders and try again."
        or "existing order" in m
        or "cancel the existing" in m
        or "cancel existing" in m
        or "cancel your open" in m
        or "in excess of current holding" in m
        or "other side of the market" in m
        or "check your open orders" in m
    )


# Default policy. Override per call if needed.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_INITIAL_BACKOFF_S = 0.4
DEFAULT_MAX_BACKOFF_S = 4.0


# ── Error classification ────────────────────────────────────────────────────

# Broker error text fragments → cleaned-up reject_reason message.
# Matched case-insensitively as substrings. Order matters — first match wins.
_USER_FIXABLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"market order.*(after|outside).*hours", re.I),
     "Market orders aren't accepted outside regular hours. Place a limit order, or try during 9:30–16:00 ET."),
    (re.compile(r"extended.hours.*(limit|DAY)", re.I),
     "Extended-hours trading needs a limit order with time-in-force = DAY."),
    (re.compile(r"asset.*not.*tradable|asset.*not.*active", re.I),
     "This asset isn't tradable on the broker right now."),
    (re.compile(r"option.*expired|expired.*option", re.I),
     "This option contract has expired."),
    (re.compile(r"contract.*not.*found|symbol.*not.*found", re.I),
     "Broker doesn't recognize that contract/symbol."),
    (re.compile(r"insufficient.*buying.power|insufficient.*funds", re.I),
     "Insufficient buying power on this account."),
    (re.compile(r"position.*not.*found|no.*position", re.I),
     "No matching position to close on this account."),
    # SnapTrade returns 403/Forbidden when an authorization is type='read'
    # and the caller tries to place an order. Distinguish that from a
    # genuine credentials-expired situation so the user gets actionable
    # guidance ("upgrade to trade permission" vs. "re-auth").
    (re.compile(r"read.?only|connection.?type.*read|trade.?permission|not.?authori[sz]ed.*(trade|place|order)", re.I),
     "This broker connection is read-only. Reconnect with trade permission, "
     "or pick a different broker (Webull/Robinhood/Schwab via SnapTrade may "
     "only support read-only — Alpaca direct always supports placement)."),
    (re.compile(r"forbidden|unauthor|permission", re.I),
     "Broker rejected the credentials (re-connect this account)."),
    (re.compile(r"halted|trading.*paused", re.I),
     "Trading is halted on this symbol."),
]

# Patterns that mean "try again later". Matched on exception type OR message.
_TRANSIENT_MESSAGE_PATTERNS = [
    re.compile(r"\b429\b|too.many.requests|rate.?limit", re.I),
    re.compile(r"\b5\d\d\b|server error|bad gateway|service unavailable|gateway timeout", re.I),
    re.compile(r"timed?.?out|timeout", re.I),
    re.compile(r"connection.*(reset|refused|aborted)|temporarily unavailable", re.I),
]

_TRANSIENT_EXCEPTION_NAMES = {
    "ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout",
    "RemoteDisconnected", "ProtocolError",
}


@dataclass
class _Classification:
    transient: bool
    clean_message: str | None  # if set, present this to the user


def classify_error(exc: BaseException) -> _Classification:
    """Look at the exception (and its message) and decide:
       - transient → caller should retry
       - clean_message → caller should surface this string as reject_reason
       - neither → unknown error, caller should re-raise."""
    msg = str(exc) or ""
    exc_name = type(exc).__name__

    # User-fixable beats transient — if the broker says "after hours", we
    # shouldn't bother retrying.
    for pattern, friendly in _USER_FIXABLE_PATTERNS:
        if pattern.search(msg):
            return _Classification(transient=False, clean_message=friendly)

    if exc_name in _TRANSIENT_EXCEPTION_NAMES:
        return _Classification(transient=True, clean_message=None)
    for pattern in _TRANSIENT_MESSAGE_PATTERNS:
        if pattern.search(msg):
            return _Classification(transient=True, clean_message=None)

    return _Classification(transient=False, clean_message=None)


# ── Recovery wrapper ────────────────────────────────────────────────────────

class RecoverableOrderError(Exception):
    """Raised when the broker rejected the order with a user-fixable cause and
    we successfully translated it into a plain message. Caller should set this
    as the order's reject_reason instead of the raw broker error string."""

    def __init__(self, friendly_message: str, original: BaseException):
        super().__init__(friendly_message)
        self.friendly_message = friendly_message
        self.original = original


def place_order_with_recovery(
    adapter: BrokerAdapter,
    request: BrokerOrderRequest,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    initial_backoff_s: float = DEFAULT_INITIAL_BACKOFF_S,
    max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
) -> BrokerOrderResult:
    """Call adapter.place_order() with retry on transient errors and clean
    messages on user-fixable errors. Re-raises unknown errors as-is.

    Raises:
        RecoverableOrderError — caller should record `.friendly_message` as
            the order's reject_reason. The original exception is preserved
            on `.original` for the audit log.
        Exception — any other broker error, unchanged.
    """
    backoff = initial_backoff_s
    attempt = 0
    last_exc: BaseException | None = None
    while attempt < max_attempts:
        attempt += 1
        try:
            return adapter.place_order(request)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            cls = classify_error(exc)
            if cls.clean_message is not None:
                raise RecoverableOrderError(cls.clean_message, exc) from exc
            if not cls.transient or attempt >= max_attempts:
                raise
            log.warning(
                "order_retry: transient broker error on attempt %d/%d, retrying in %.1fs: %s",
                attempt, max_attempts, backoff, str(exc)[:200],
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff_s)
    # Should be unreachable: we always raise on the final attempt above.
    assert last_exc is not None
    raise last_exc
