"""Is this a symbol that trades?

The Discord parsers read chatter as trades now and then: "Haha I bought 2 cons"
becomes BUY HAHA, "Yeah mine up 30%" becomes BUY YEAH. Order History's Discord
tab listed them beside real alerts (QA 2026-10-22). This answers "is HAHA a
real ticker" from Alpaca's list of active US equities, fetched with the DATA
account's keys once a day and kept in Redis for every process.

Index option roots (SPX, XSP, NDX …) are not equities but are real underlyings,
so they always count.

None — "can't tell" — when the list can't be had (no data key, Alpaca down).
A caller must then keep the row: hiding a real alert is worse than showing a
misread one.
"""
from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger(__name__)

INDEX_ROOTS = frozenset({
    "SPX", "SPXW", "XSP", "NDX", "NDXP", "RUT", "RUTW", "VIX", "VIXW", "DJX", "OEX", "XEO", "MRUT",
})
_TTL_S = 24 * 3600
# After a failed fetch, don't retry on every page load.
_RETRY_S = 10 * 60
_REDIS_KEY = "market:listed-symbols:v1"

_lock = threading.Lock()
_cache: tuple[float, frozenset[str]] | None = None
_failed_at: float | None = None


def _redis():
    from app.services.redis_client import get_sync_redis  # noqa: PLC0415

    return get_sync_redis()


def _fetch() -> frozenset[str] | None:
    """Every active US equity symbol, from Alpaca (data account keys)."""
    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    if not (s.alpaca_data_api_key and s.alpaca_data_api_secret):
        return None
    from alpaca.trading.client import TradingClient  # noqa: PLC0415
    from alpaca.trading.enums import AssetClass, AssetStatus  # noqa: PLC0415
    from alpaca.trading.requests import GetAssetsRequest  # noqa: PLC0415

    req = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    # Paper keys start "PK" and answer only on the paper endpoint; try the
    # likely one first, then the other.
    paper_first = s.alpaca_data_api_key.upper().startswith("PK")
    last: Exception | None = None
    for paper in (paper_first, not paper_first):
        try:
            assets = TradingClient(s.alpaca_data_api_key, s.alpaca_data_api_secret, paper=paper).get_all_assets(req)
            syms = frozenset(str(a.symbol).upper() for a in assets if getattr(a, "symbol", None))
            if syms:
                return syms
        except Exception as exc:  # noqa: BLE001
            last = exc
    if last is not None:
        log.warning("market_symbols: could not list Alpaca assets: %s", last)
    return None


def listed() -> frozenset[str] | None:
    """Active US equity symbols, or None when they can't be had."""
    global _cache, _failed_at
    now = time.time()
    with _lock:
        if _cache is not None and now - _cache[0] < _TTL_S:
            return _cache[1]
        try:
            raw = _redis().get(_REDIS_KEY)
            if raw:
                data = json.loads(raw)
                if now - float(data["at"]) < _TTL_S:
                    _cache = (float(data["at"]), frozenset(data["symbols"]))
                    return _cache[1]
        except Exception:  # noqa: BLE001
            pass
        if _failed_at is not None and now - _failed_at < _RETRY_S:
            return None
        syms = _fetch()
        if not syms:
            _failed_at = now
            return None
        _cache, _failed_at = (now, syms), None
        try:
            _redis().set(_REDIS_KEY, json.dumps({"at": now, "symbols": sorted(syms)}), ex=_TTL_S)
        except Exception:  # noqa: BLE001
            pass
        return syms


def is_listed(symbol: str | None) -> bool | None:
    """True for a symbol that trades, False for one that doesn't, None when it
    can't be told."""
    sym = (symbol or "").upper().strip().lstrip("$")
    if not sym:
        return False
    if sym in INDEX_ROOTS:
        return True
    syms = listed()
    if syms is None:
        return None
    return sym in syms


__all__ = ["INDEX_ROOTS", "is_listed", "listed"]
