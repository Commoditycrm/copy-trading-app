"""Centralized live market-data stream (phase 1).

One Alpaca SIP WebSocket — authenticated with a dedicated PAID account's keys,
NEVER a subscriber's — streams live stock quotes into Redis. Every user, on every
broker, then reads the SAME price from the cache (``get_live_price``) instead of
each broker polling its own quote endpoint. This is the ingest + central-store
half; rewiring the price readers to ``get_live_price`` is phase 2.

Why it scales: the feed carries the UNION of symbols anyone holds/trades (deduped,
~dozens), so the cost is O(unique symbols) — flat whether there are 100 or 10,000
subscribers — versus today's O(subscribers × symbols) per-broker polling that
blows SnapTrade's shared 250/min quota at ~100 subs.

Worker-only. Gated behind ``settings.alpaca_market_stream_enabled`` (default OFF)
plus non-empty keys, so it is inert until explicitly enabled after QA.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models.order import InstrumentType, Order, OrderStatus

log = logging.getLogger(__name__)


class _ThrottleLogFilter(logging.Filter):
    """Collapse identical repeated log lines to at most one per interval. The
    alpaca-py data websocket retries internally at full speed, so a bad/
    unentitled key spams 'auth failed' every second — this quiets it to once a
    minute without hiding a genuine new error."""

    def __init__(self, min_interval_s: float = 60.0) -> None:
        super().__init__()
        self._iv = min_interval_s
        self._last: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            key = record.getMessage()[:80]
        except Exception:  # noqa: BLE001
            return True
        now = time.monotonic()
        if now - self._last.get(key, 0.0) < self._iv:
            return False
        self._last[key] = now
        return True


# Auth-failure backoff. alpaca-py's data websocket retries internally at full
# speed on a bad/unentitled key, which trips Alpaca's 429 connection-rate limit.
# We watch the stream logger for auth/429 markers and, after a few, stop the
# socket and refuse to restart for a growing interval — so an invalid key can't
# hammer the endpoint. Cleared the moment a real quote arrives (_set_price).
_AUTH_FAIL_MARKERS = ("auth failed", "forbidden", "not authorized", "unauthorized", "http 429", "connection limit")
_AUTH_FAIL_THRESHOLD = 3
_AUTH_BACKOFF_BASE_S = 60.0
_AUTH_BACKOFF_MAX_S = 600.0
_auth_fail_count = 0
_auth_backoff_until = 0.0  # monotonic; don't (re)start the stream before this


class _AuthFailWatcher(logging.Filter):
    """Filter (never suppresses) that counts auth/429 failures on the alpaca data
    websocket and arms a backoff once they cross a threshold, stopping the socket
    so alpaca-py's internal retry loop can't keep hammering. Added BEFORE the
    throttle filter so it sees every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        global _auth_fail_count, _auth_backoff_until
        try:
            msg = record.getMessage().lower()
        except Exception:  # noqa: BLE001
            return True
        if any(m in msg for m in _AUTH_FAIL_MARKERS):
            _auth_fail_count += 1
            if _auth_fail_count >= _AUTH_FAIL_THRESHOLD and _auth_backoff_until <= time.monotonic():
                backoff = min(_AUTH_BACKOFF_MAX_S,
                              _AUTH_BACKOFF_BASE_S * (2 ** min(_auth_fail_count - _AUTH_FAIL_THRESHOLD, 4)))
                _auth_backoff_until = time.monotonic() + backoff
                try:
                    _stop_stream()
                except Exception:  # noqa: BLE001
                    pass
                log.warning("market_data_stream: auth failing (%d) — pausing the stream %.0fs "
                            "to avoid a 429 storm; check the data key/entitlement",
                            _auth_fail_count, backoff)
        return True


_throttle_installed = False


def _install_log_throttle() -> None:
    """Attach the auth watcher + throttle to the noisy alpaca data-websocket
    logger, once. Watcher first so it sees records the throttle would drop."""
    global _throttle_installed
    if _throttle_installed:
        return
    lg = logging.getLogger("alpaca.data.live.websocket")
    lg.addFilter(_AuthFailWatcher())
    lg.addFilter(_ThrottleLogFilter(60.0))
    _throttle_installed = True


# Redis key per symbol; short TTL so a symbol we stop streaming goes stale on its
# own rather than serving a frozen price forever.
_PRICE_KEY = "mdprice:{}"
_PRICE_TTL_S = 300
# A cached price older than this is treated as stale (get_live_price returns None,
# so the caller falls back to its existing REST path). Streams tick continuously
# for liquid names; a gap this long means the feed or the symbol went quiet.
_MAX_AGE_S = 15.0
# How often the supervisor re-computes the held/traded symbol set.
_REFRESH_S = 60.0

_WORKING = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)

_task: "asyncio.Task | None" = None
_stream: Any = None            # the live StockDataStream (for stop on restart)
_stream_task: "asyncio.Task | None" = None
_current_symbols: frozenset[str] = frozenset()
# Bumped on every (re)start so a late callback from an old stream is ignored.
_generation = 0

# Parallel OPTION stream (Alpaca OPRA). Same cache + _set_price, keyed by the OCC
# symbol; separate connection/lifecycle because options use their own websocket.
_opt_stream: Any = None
_opt_stream_task: "asyncio.Task | None" = None
_opt_current_symbols: frozenset[str] = frozenset()
_opt_generation = 0

# On-demand watch (trade panel): symbols a user is actively viewing get streamed
# even if nobody holds them, via a short-TTL Redis key subscribed INCREMENTALLY
# on the running stream (no restart → the held/working feed never flaps). The
# current quote handlers are stashed here so the supervisor can add/drop watched
# symbols on the live client; the subscribed watch sets track what we've added.
_WATCH_KEY = "mdwatch:{}"
_WATCH_TTL_S = 120
_stock_handler: Any = None
_opt_handler: Any = None
_watch_stock_subscribed: set[str] = set()
_watch_opt_subscribed: set[str] = set()
# OCC = root(1-6) + YYMMDD + C/P + strike(8). Anything else is treated as a stock.
_OCC_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,5}\d{6}[CP]\d{8}$")
# Base (held/working) sets are recomputed off the DB at most this often; watches
# reconcile every supervisor pass so a typed symbol streams within a pass.
_WATCH_PASS_S = 5.0


def _enabled() -> bool:
    from app.config import get_settings  # noqa: PLC0415
    s = get_settings()
    return bool(
        s.alpaca_market_stream_enabled
        and s.alpaca_data_api_key
        and s.alpaca_data_api_secret
    )


# ── central store: Redis price cache ────────────────────────────────────────
# Per-symbol throttle for the SSE price-tick broadcast: quotes tick many times a
# second (esp. options), but the screen only needs ~1 update/sec/symbol.
_TICK_MIN_INTERVAL_S = 1.0
_last_tick_at: dict[str, float] = {}


def _set_price(symbol: str, price: Decimal, bid: Any = None, ask: Any = None) -> None:
    from app.services.redis_client import get_sync_redis  # noqa: PLC0415
    # A real quote means auth succeeded — clear any auth-failure backoff.
    global _auth_fail_count, _auth_backoff_until
    if _auth_fail_count or _auth_backoff_until:
        _auth_fail_count = 0
        _auth_backoff_until = 0.0
    sym = symbol.upper()
    try:
        payload = json.dumps({"p": str(price), "t": int(time.time() * 1000)})
        get_sync_redis().set(_PRICE_KEY.format(sym), payload, ex=_PRICE_TTL_S)
    except Exception:  # noqa: BLE001
        pass  # best-effort cache; never let a Redis blip kill the stream
    # Phase 4: push the tick to open screens over SSE, throttled per symbol. The
    # tick carries bid/ask too so the trade panel's quote panel ticks live (the
    # cache above stays mid-only — get_live_price wants one number).
    now = time.monotonic()
    if now - _last_tick_at.get(sym, 0.0) >= _TICK_MIN_INTERVAL_S:
        _last_tick_at[sym] = now
        try:
            from app.services import events  # noqa: PLC0415
            b = str(bid) if bid not in (None, "") and float(bid) > 0 else None
            a = str(ask) if ask not in (None, "") and float(ask) > 0 else None
            events.publish_price(sym, str(price), b, a)
        except Exception:  # noqa: BLE001
            pass


def get_live_price(symbol: str, max_age_s: float = _MAX_AGE_S) -> Decimal | None:
    """The centralized live price for ``symbol`` (stocks), or None if we have no
    fresh cached quote — in which case the caller uses its existing REST path.
    Read by any user, any broker; safe to call from sync or async code."""
    from app.services.redis_client import get_sync_redis  # noqa: PLC0415
    try:
        raw = get_sync_redis().get(_PRICE_KEY.format(symbol.upper()))
        if not raw:
            return None
        obj = json.loads(raw)
        age = time.time() - (float(obj["t"]) / 1000.0)
        if age > max_age_s:
            return None
        return Decimal(str(obj["p"]))
    except (InvalidOperation, ValueError, KeyError, TypeError, Exception):  # noqa: BLE001
        return None


# ── on-demand watch (trade panel) ───────────────────────────────────────────
def add_watch(symbols: list[str]) -> None:
    """Mark symbols (tickers or OCC) as actively watched so the stream picks them
    up on demand. Idempotent; refreshes the TTL. Called by the watch endpoint on
    a heartbeat while a trade-panel symbol is selected."""
    from app.services.redis_client import get_sync_redis  # noqa: PLC0415
    try:
        r = get_sync_redis()
        for s in symbols:
            sym = (s or "").upper().strip()
            if sym:
                r.set(_WATCH_KEY.format(sym), "1", ex=_WATCH_TTL_S)
    except Exception:  # noqa: BLE001
        pass


def _read_watch() -> set[str]:
    from app.services.redis_client import get_sync_redis  # noqa: PLC0415
    out: set[str] = set()
    try:
        for k in get_sync_redis().scan_iter(_WATCH_KEY.format("*")):
            ks = k if isinstance(k, str) else k.decode()
            out.add(ks.split(":", 1)[1].upper())
    except Exception:  # noqa: BLE001
        pass
    return out


def fetch_rest_quote(symbol: str) -> Decimal | None:
    """One-shot REST quote for the instant trade-panel seed — so a price shows the
    moment a symbol is typed, before the stream warms up. Uses the DEDICATED data
    key (never a subscriber's broker) and writes the result into the cache, so
    the very first paint is a real price and useLivePrice has it immediately."""
    from app.config import get_settings  # noqa: PLC0415
    s = get_settings()
    if not (s.alpaca_data_api_key and s.alpaca_data_api_secret):
        return None
    sym = symbol.upper().strip()
    try:
        if _OCC_RE.match(sym):
            from alpaca.data.historical.option import OptionHistoricalDataClient  # noqa: PLC0415
            from alpaca.data.requests import OptionLatestQuoteRequest  # noqa: PLC0415
            c = OptionHistoricalDataClient(s.alpaca_data_api_key, s.alpaca_data_api_secret)
            q = c.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=sym))
        else:
            from alpaca.data.enums import DataFeed  # noqa: PLC0415
            from alpaca.data.historical.stock import StockHistoricalDataClient  # noqa: PLC0415
            from alpaca.data.requests import StockLatestQuoteRequest  # noqa: PLC0415
            feed = DataFeed.SIP if s.alpaca_data_feed.lower() == "sip" else DataFeed.IEX
            c = StockHistoricalDataClient(s.alpaca_data_api_key, s.alpaca_data_api_secret)
            q = c.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym, feed=feed))
        v = q.get(sym) if isinstance(q, dict) else q
        px = _quote_mid(v) if v is not None else None
        if px is not None:
            _set_price(sym, px)
        return px
    except Exception:  # noqa: BLE001
        log.warning("market_data_stream: fetch_rest_quote(%s) failed", sym)
        return None


# ── symbol set: everything anyone holds or is working ───────────────────────
def _compute_symbols() -> set[str]:
    """Union of STOCK symbols with a live net position (any user) or a working
    order — the only symbols the stream subscribes to. Defensive so a bad query
    never crashes the supervisor (it just keeps the previous set)."""
    from sqlalchemy import case, func  # noqa: PLC0415
    from app.models.order import OrderSide  # noqa: PLC0415

    syms: set[str] = set()
    with SessionLocal() as db:
        # Held: net filled qty per (user, symbol) != 0 → still holding it.
        net = func.sum(
            case((Order.side == OrderSide.BUY, Order.filled_quantity),
                 else_=-Order.filled_quantity)
        )
        held_rows = db.execute(
            select(Order.symbol)
            .where(
                Order.instrument_type == InstrumentType.STOCK,
                Order.status.in_((OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)),
            )
            .group_by(Order.user_id, Order.symbol)
            .having(net != 0)
        ).scalars().all()
        # About-to-hold: any working stock order.
        working_rows = db.execute(
            select(Order.symbol)
            .where(
                Order.instrument_type == InstrumentType.STOCK,
                Order.status.in_(_WORKING),
            )
            .distinct()
        ).scalars().all()
    for sym in list(held_rows) + list(working_rows):
        if sym:
            syms.add(sym.upper())
    return syms


def _build_occ(symbol: str | None, expiry: Any, strike: Any, right: Any) -> str | None:
    """OCC symbol Alpaca's OPRA feed uses: ROOT + YYMMDD + C/P + strike*1000 (8
    digits), root NOT zero-padded — e.g. INQQ261016C00013000. Must match the OCC
    the frontend builds so cache writes and reads line up."""
    try:
        if not symbol or expiry is None or strike is None or not right:
            return None
        yymmdd = expiry.strftime("%y%m%d")
        cp = "C" if "call" in str(right).lower() else "P"
        strike_int = int(round(float(strike) * 1000))
        return f"{symbol.upper()}{yymmdd}{cp}{strike_int:08d}"
    except Exception:  # noqa: BLE001
        return None


def _compute_option_symbols() -> set[str]:
    """OCC symbols for options anyone holds (net != 0) or is working — the option
    stream's subscription set. Same held/working logic as _compute_symbols, keyed
    on the full contract (symbol+expiry+strike+right)."""
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from sqlalchemy import case, func  # noqa: PLC0415
    from app.models.order import OrderSide  # noqa: PLC0415

    # Never stream expired contracts — they can't quote and just waste OPRA
    # subscription slots. One-day margin so a same-day expiry (still tradeable
    # through its ET close) is never dropped by a UTC-date comparison.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    occs: set[str] = set()
    cols = (Order.symbol, Order.option_expiry, Order.option_strike, Order.option_right)
    with SessionLocal() as db:
        net = func.sum(
            case((Order.side == OrderSide.BUY, Order.filled_quantity),
                 else_=-Order.filled_quantity)
        )
        held_rows = db.execute(
            select(*cols)
            .where(
                Order.instrument_type == InstrumentType.OPTION,
                Order.status.in_((OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)),
                Order.option_expiry >= cutoff,
            )
            .group_by(Order.user_id, *cols)
            .having(net != 0)
        ).all()
        working_rows = db.execute(
            select(*cols)
            .where(
                Order.instrument_type == InstrumentType.OPTION,
                Order.status.in_(_WORKING),
                Order.option_expiry >= cutoff,
            )
            .distinct()
        ).all()
    for sym, exp, strike, right in list(held_rows) + list(working_rows):
        occ = _build_occ(sym, exp, strike, right)
        if occ:
            occs.add(occ)
    return occs


# ── the stream ──────────────────────────────────────────────────────────────
def _quote_mid(q: Any) -> Decimal | None:
    bid = getattr(q, "bid_price", None)
    ask = getattr(q, "ask_price", None)
    try:
        if bid and ask and bid > 0 and ask > 0:
            return (Decimal(str(bid)) + Decimal(str(ask))) / Decimal(2)
        if ask and ask > 0:
            return Decimal(str(ask))
        if bid and bid > 0:
            return Decimal(str(bid))
    except (InvalidOperation, ValueError):
        return None
    return None


async def _run_stream(symbols: frozenset[str], generation: int) -> None:
    """Build + run the Alpaca StockDataStream for ``symbols`` (blocking run in a
    thread, like the Webull gRPC listener). Writes each quote's mid to Redis."""
    global _stream
    from alpaca.data.enums import DataFeed  # noqa: PLC0415
    from alpaca.data.live import StockDataStream  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    feed = DataFeed.SIP if s.alpaca_data_feed.lower() == "sip" else DataFeed.IEX
    client = StockDataStream(
        s.alpaca_data_api_key, s.alpaca_data_api_secret, feed=feed,
    )
    _stream = client

    async def _on_quote(q: Any) -> None:
        if generation != _generation:
            return  # a newer stream superseded us
        sym = getattr(q, "symbol", None)
        if not sym:
            return
        px = _quote_mid(q)
        if px is not None:
            _set_price(sym, px, getattr(q, "bid_price", None), getattr(q, "ask_price", None))

    global _stock_handler
    _stock_handler = _on_quote          # for incremental watch subscribes
    client.subscribe_quotes(_on_quote, *symbols)
    log.info("market_data_stream: subscribing to %d symbols (feed=%s)", len(symbols), feed)
    # run() blocks until the socket closes (or client.stop() is called on restart).
    await asyncio.to_thread(client.run)


def _stop_stream() -> None:
    global _stream
    c = _stream
    _stream = None
    if c is not None:
        try:
            c.stop()
        except Exception:  # noqa: BLE001
            pass


async def _run_option_stream(symbols: frozenset[str], generation: int) -> None:
    """Build + run the Alpaca OptionDataStream (OPRA) for ``symbols`` (OCC), same
    shape as the stock stream. Writes each contract's quote mid to the shared
    cache under its OCC key, so a position row / trade ticket keyed on that OCC
    ticks live."""
    global _opt_stream
    from alpaca.data.enums import OptionsFeed  # noqa: PLC0415
    from alpaca.data.live.option import OptionDataStream  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    client = OptionDataStream(
        s.alpaca_data_api_key, s.alpaca_data_api_secret, feed=OptionsFeed.OPRA,
    )
    _opt_stream = client

    async def _on_quote(q: Any) -> None:
        if generation != _opt_generation:
            return
        sym = getattr(q, "symbol", None)
        if not sym:
            return
        px = _quote_mid(q)
        if px is not None:
            _set_price(sym, px, getattr(q, "bid_price", None), getattr(q, "ask_price", None))

    global _opt_handler
    _opt_handler = _on_quote            # for incremental watch subscribes
    client.subscribe_quotes(_on_quote, *symbols)
    log.info("market_data_stream: subscribing to %d OPTION symbols (OPRA)", len(symbols))
    await asyncio.to_thread(client.run)


def _stop_option_stream() -> None:
    global _opt_stream
    c = _opt_stream
    _opt_stream = None
    if c is not None:
        try:
            c.stop()
        except Exception:  # noqa: BLE001
            pass


async def _reconcile_watches() -> None:
    """Incrementally add/drop trade-panel watched symbols on the RUNNING streams
    (no restart). Watched symbols already in the base held/working set are skipped
    — they stream anyway. Runs every supervisor pass so a typed symbol streams
    within seconds."""
    global _watch_stock_subscribed, _watch_opt_subscribed
    watch = await asyncio.to_thread(_read_watch)
    want_stock = {s for s in watch if not _OCC_RE.match(s)} - _current_symbols
    want_opt = {s for s in watch if _OCC_RE.match(s)} - _opt_current_symbols

    if _stream is not None and _stock_handler is not None:
        add = want_stock - _watch_stock_subscribed
        rem = _watch_stock_subscribed - want_stock
        if add:
            try:
                await asyncio.to_thread(_stream.subscribe_quotes, _stock_handler, *add)
                _watch_stock_subscribed |= add
                log.info("market_data_stream: watch +%d stock(s): %s", len(add), sorted(add))
            except Exception:  # noqa: BLE001
                log.exception("market_data_stream: watch stock subscribe failed")
        if rem:
            try:
                await asyncio.to_thread(_stream.unsubscribe_quotes, *rem)
            except Exception:  # noqa: BLE001
                pass
            _watch_stock_subscribed -= rem

    if _opt_stream is not None and _opt_handler is not None:
        add = want_opt - _watch_opt_subscribed
        rem = _watch_opt_subscribed - want_opt
        if add:
            try:
                await asyncio.to_thread(_opt_stream.subscribe_quotes, _opt_handler, *add)
                _watch_opt_subscribed |= add
                log.info("market_data_stream: watch +%d option(s): %s", len(add), sorted(add))
            except Exception:  # noqa: BLE001
                log.exception("market_data_stream: watch option subscribe failed")
        if rem:
            try:
                await asyncio.to_thread(_opt_stream.unsubscribe_quotes, *rem)
            except Exception:  # noqa: BLE001
                pass
            _watch_opt_subscribed -= rem


async def _supervise() -> None:
    """Recompute the base held/working set every _REFRESH_S and (re)start the
    streams on change or death; reconcile trade-panel watches every pass so a
    typed symbol streams within seconds without restarting the base feed."""
    global _stream_task, _current_symbols, _generation
    global _opt_stream_task, _opt_current_symbols, _opt_generation
    global _watch_stock_subscribed, _watch_opt_subscribed
    last_base = 0.0
    while True:
        try:
            if _enabled():
                if time.monotonic() < _auth_backoff_until:
                    # Auth is failing (bad/unentitled key). Hold both sockets down
                    # so alpaca-py's internal retry can't hammer → no 429 storm.
                    _stop_stream()
                    _stop_option_stream()
                    _watch_stock_subscribed = set()
                    _watch_opt_subscribed = set()
                else:
                    now = time.monotonic()
                    due = now - last_base >= _REFRESH_S
                    if due:
                        last_base = now
                    loop = asyncio.get_running_loop()
                    # ── stock base: recompute on the slow cadence; restart on
                    #    change OR death (checked every pass via the cached set) ──
                    stock_syms = (
                        frozenset(await asyncio.to_thread(_compute_symbols)) if due
                        else _current_symbols
                    )
                    if stock_syms and (
                        stock_syms != _current_symbols
                        or _stream_task is None or _stream_task.done()
                    ):
                        _stop_stream()
                        if _stream_task is not None and not _stream_task.done():
                            _stream_task.cancel()
                        _generation += 1
                        _current_symbols = stock_syms
                        _watch_stock_subscribed = set()   # new client → re-add watches
                        _stream_task = loop.create_task(_run_stream(stock_syms, _generation))
                    elif due and not stock_syms:
                        _stop_stream()
                        _current_symbols = frozenset()
                        _watch_stock_subscribed = set()
                    # ── option base (OPRA) ──
                    opt_syms = (
                        frozenset(await asyncio.to_thread(_compute_option_symbols)) if due
                        else _opt_current_symbols
                    )
                    if opt_syms and (
                        opt_syms != _opt_current_symbols
                        or _opt_stream_task is None or _opt_stream_task.done()
                    ):
                        _stop_option_stream()
                        if _opt_stream_task is not None and not _opt_stream_task.done():
                            _opt_stream_task.cancel()
                        _opt_generation += 1
                        _opt_current_symbols = opt_syms
                        _watch_opt_subscribed = set()
                        _opt_stream_task = loop.create_task(_run_option_stream(opt_syms, _opt_generation))
                    elif due and not opt_syms:
                        _stop_option_stream()
                        _opt_current_symbols = frozenset()
                        _watch_opt_subscribed = set()
                    # ── trade-panel watches (every pass, incremental) ──
                    await _reconcile_watches()
            else:
                _stop_stream()
                _stop_option_stream()
                _current_symbols = frozenset()
                _opt_current_symbols = frozenset()
                _watch_stock_subscribed = set()
                _watch_opt_subscribed = set()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("market_data_stream supervisor pass failed")
        await asyncio.sleep(_WATCH_PASS_S)


def start_market_data_stream() -> None:
    """Spawn the supervisor. Idempotent, worker-only. Safe to call
    unconditionally — it re-checks the flag+keys each pass and runs nothing while
    disabled."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("market_data_stream: no running loop; not starting")
        return
    _install_log_throttle()
    _task = loop.create_task(_supervise())
    log.info("market_data_stream: supervisor started (enabled=%s)", _enabled())


async def stop_market_data_stream() -> None:
    global _task, _stream_task, _opt_stream_task
    _stop_stream()
    _stop_option_stream()
    for t in (_stream_task, _opt_stream_task, _task):
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    _task = None
    _stream_task = None
    _opt_stream_task = None


__all__ = [
    "start_market_data_stream", "stop_market_data_stream",
    "get_live_price", "add_watch",
]
