"""On-demand live-quote endpoint for the trade panel.

The centralized stream only carries symbols someone holds or is working. When a
trader types a symbol into the trade panel we (a) register it as "watched" so the
stream subscribes to it within a few seconds, and (b) return an instant REST
quote so a price shows immediately. From then on the SSE price ticks drive the
live update, exactly like the positions table.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import current_user
from app.models.user import User
from app.services import market_data_stream as mds

router = APIRouter(prefix="/api/market-data", tags=["market-data"])


class WatchBody(BaseModel):
    # Tickers ("AAPL") or OCC option symbols ("AAPL260925C00250000").
    symbols: list[str]


@router.post("/watch")
def watch(body: WatchBody, _user: User = Depends(current_user)) -> dict:
    """Register symbols as watched (heartbeat) and return a current price for
    each — cache first, one-shot REST otherwise — so the trade panel paints a
    price instantly and then ticks live off the stream."""
    syms = [s.upper().strip() for s in body.symbols if s and s.strip()][:25]
    if syms:
        mds.add_watch(syms)
    prices: dict[str, str | None] = {}
    for sym in syms:
        px = mds.get_live_price(sym, max_age_s=30.0)
        if px is None:
            px = mds.fetch_rest_quote(sym)
        prices[sym] = str(px) if px is not None else None
    return {"prices": prices}
