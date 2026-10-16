"""The price stops and trailing exits are judged at: Alpaca's live quote.

Every stop and trailing decision used to read the price off the BROKER's
position — on Webull, a positions call against a 2-per-2s limit shared with
the Positions page, the P&L poller and auto-trim. When that call was refused
the decision had no price, and when it went through the price was whatever the
broker last marked. The same quote is streamed for free from the Alpaca DATA
account (services/market_data_stream — its own keys, never a trader's), so
decisions take it from there:

  1. a Simulated Prices pin, when that test feature is on;
  2. the streamed quote from the cache (fresh within 15s);
  3. one REST quote from the Alpaca data account (which then seeds the cache);
  4. only then the broker's own mark, if the caller has one.

The broker is still asked what is HELD — a price says nothing about that.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

log = logging.getLogger(__name__)


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d > 0 else None


def quote_key(symbol: str, strike: Any, right: Any, expiry: Any) -> str | None:
    """The symbol the Alpaca feed knows this contract by: the OCC for an option,
    the ticker for a stock."""
    from app.services import market_data_stream  # noqa: PLC0415

    if strike is None:
        return (symbol or "").upper() or None
    return market_data_stream._build_occ(symbol, expiry, strike, getattr(right, "value", right))


def contract_mark(symbol: str, strike: Any = None, right: Any = None, expiry: Any = None, *,
                  user_id: Any = None, pos: Any = None, fallback: Any = None) -> Decimal | None:
    """The live price for a contract (or stock), Alpaca first. None when no
    source has one."""
    if user_id is not None and pos is not None:
        from app.services import price_override  # noqa: PLC0415

        pinned = price_override.apply_to(user_id, pos)
        if pinned is not None:
            return pinned
    key = quote_key(symbol, strike, right, expiry)
    if key:
        from app.services import market_data_stream  # noqa: PLC0415

        try:
            px = market_data_stream.get_live_price(key, max_age_s=15.0)
            if px is None:
                px = market_data_stream.fetch_rest_quote(key)
        except Exception:  # noqa: BLE001
            log.debug("live mark: Alpaca quote for %s unavailable", key, exc_info=True)
            px = None
        if px is not None and px > 0:
            return px
    if fallback is None and pos is not None:
        fallback = getattr(pos, "current_price", None)
    return _dec(fallback)


def position_mark(pos: Any, user_id: Any = None) -> Decimal | None:
    """contract_mark for a broker position, falling back to the broker's mark."""
    return contract_mark(
        pos.symbol, getattr(pos, "option_strike", None), getattr(pos, "option_right", None),
        getattr(pos, "option_expiry", None), user_id=user_id, pos=pos,
    )


__all__ = ["contract_mark", "position_mark", "quote_key"]
