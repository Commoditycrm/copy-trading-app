"""Which contract a Discord alert row should show a live price for.

The Order History "Discord" tab shows a Current price beside each alert so the
trader can decide on one awaiting approval. The page priced a row only when the
alert named a WHOLE contract — and many don't: "✂️ $SPY 770c +43%" names no
expiry, "SL hit sir 50%" names nothing at all. Those rows showed "—".

This works out the contract the way execution would, from our own records and
the Alpaca DATA account — never the trader's broker, so it costs no Webull call:

  * a whole contract                       -> that contract
  * symbol + strike + right, no expiry     -> the channel's open position on it,
                                              else the nearest listed expiry
  * nothing named (an exit)                -> the channel's newest open position

Only for rows worth a live price: posted in the last hour, and not already
submitted or accepted (the decision the price informs is behind them).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=1)
# Listed expiries are looked for this far ahead, as execution does.
_EXPIRY_SEARCH = timedelta(days=21)


def tracked(m, now: datetime | None = None) -> bool:
    """Does this alert row get a live price? Posted within the hour, and not
    yet an order (submitted) or cleared for one (accepted). Mirrors how the
    Discord tab labels a row: a failed alert reads "rejected" even if it had
    been approved."""
    now = now or datetime.now(timezone.utc)
    posted = getattr(m, "posted_at", None) or getattr(m, "created_at", None)
    if posted is None:
        return False
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    if now - posted > WINDOW:
        return False
    status = getattr(m.status, "value", m.status)
    if status == "order_created":
        return False                                   # submitted
    failed = status in ("invalid", "order_failed")
    decision = getattr(m.decision, "value", m.decision) if m.decision else None
    return failed or decision != "approved"            # accepted is out


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _date(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _right(v) -> str | None:
    v = (getattr(v, "value", v) or "").lower()
    return v if v in ("call", "put") else None


def _occ(symbol: str, expiry: date, strike: Decimal, right: str) -> str:
    from app.brokers.alpaca import build_occ_symbol  # noqa: PLC0415

    return build_occ_symbol(symbol, expiry, strike, right)


def _open_positions(db: Session, user_id, source_id) -> list:
    """This channel's open Discord positions (live ladder records), newest
    first. Our own records — no broker call."""
    from app.models.discord_position_guard import DiscordPositionGuard as G  # noqa: PLC0415

    return list(db.execute(
        select(G).where(
            G.user_id == user_id, G.source_id == source_id, G.closed_at.is_(None),
            G.option_strike.isnot(None), G.option_expiry.isnot(None),
        ).order_by(G.created_at.desc())
    ).scalars())


def _nearest_expiry(symbol: str, strike: Decimal, right: str, cache: dict) -> date | None:
    key = (symbol, strike, right)
    if key not in cache:
        from app.models.order import OptionRight  # noqa: PLC0415
        from app.services import discord_execution as ex, market_hours  # noqa: PLC0415

        today = market_hours.now_et().date()
        try:
            found = ex._data_account_expiries(
                ex._chain_root(symbol), strike,
                OptionRight.CALL if right == "call" else OptionRight.PUT,
                today, today + _EXPIRY_SEARCH,
            )
        except Exception:  # noqa: BLE001 — a price is never worth failing the page for
            log.warning("discord prices: no option chain for %s %s%s", symbol, strike, right, exc_info=True)
            found = None
        cache[key] = found[0] if found else None
    return cache[key]


def live_symbol(db: Session, user_id, m, sig: dict, *, cache: dict,
                now: datetime | None = None) -> str | None:
    """The symbol to price this alert row by (an OCC option symbol, or a stock
    ticker), or None when it gets no live price."""
    if not sig or not tracked(m, now):
        return None
    symbol = (sig.get("symbol") or "").upper().strip() or None
    if (sig.get("asset_type") or "").upper() == "STOCK":
        return symbol
    strike, right, expiry = _dec(sig.get("strike")), _right(sig.get("option_type")), _date(sig.get("expiration"))
    if symbol and strike is not None and right and expiry:
        return _occ(symbol, expiry, strike, right)

    # Fill what the alert left out from the channel's open positions.
    opens = cache.setdefault(("opens", m.source_id), None)
    if opens is None:
        opens = cache[("opens", m.source_id)] = _open_positions(db, user_id, m.source_id)
    for g in opens:
        if symbol and (g.symbol or "").upper() != symbol:
            continue
        if strike is not None and Decimal(str(g.option_strike)) != strike:
            continue
        if right and _right(g.option_right) != right:
            continue
        return _occ(g.symbol, g.option_expiry, Decimal(str(g.option_strike)), _right(g.option_right))

    # Not held: an entry that named no expiry takes the nearest listed one.
    if symbol and strike is not None and right:
        nearest = _nearest_expiry(symbol, strike, right, cache)
        if nearest:
            return _occ(symbol, nearest, strike, right)
    return None


__all__ = ["WINDOW", "live_symbol", "tracked"]
