"""Webull MQTT market-data stream — SECOND source for the central price cache.

A quote-entitled Webull OpenAPI app_key streams live stock quotes over Webull's
MQTT feed into the SAME Redis cache the Alpaca stream fills (``mdprice:{symbol}``,
via ``market_data_stream._set_price``). So Alpaca and Webull become two sources
for one cache: ``get_live_price`` and every downstream reader are unchanged, and
if the Alpaca stream is down this keeps the cache fresh.

Worker-only. Gated behind ``settings.webull_market_stream_enabled`` (default OFF)
plus non-empty keys. Uses a DEDICATED quote-entitled Webull account — never a
subscriber's.

NOTE: Webull's streaming SDK is low-level/undocumented. The connect → session →
subscribe → decode flow here is a best-effort first version; the exact sub-type
value and the decoded-quote field names are finalized against live keys (the
handler logs the first raw message so we can see the real shape). Everything
around it (symbol set, cache write, supervisor, lifecycle) is solid.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.market_data_stream import _compute_symbols, _set_price

log = logging.getLogger(__name__)

_REFRESH_S = 60.0
# On repeated connect failures (e.g. bad/entitlement-less keys) back off the
# retry cadence instead of hammering + spamming logs; reset on a good connect.
_BACKOFF_MAX = 300.0
_task: "asyncio.Task | None" = None
_client: Any = None
_current_symbols: frozenset[str] = frozenset()
_generation = 0
_fail_streak = 0
_logged_sample = False  # one-time raw-message dump to learn the payload shape


def _is_connected() -> bool:
    """True when the MQTT client exists and its socket is live. The SDK creates
    the client with reconnect_on_failure=False, so a dropped connection stays
    dead until the supervisor restarts it — this is how we detect that."""
    c = _client
    try:
        return bool(c is not None and c.is_connected())
    except Exception:  # noqa: BLE001
        return False


def _enabled() -> bool:
    from app.config import get_settings  # noqa: PLC0415
    s = get_settings()
    return bool(
        s.webull_market_stream_enabled
        and s.webull_data_app_key
        and s.webull_data_app_secret
    )


def _extract(msg: Any) -> tuple[str | None, Decimal | None]:
    """Pull (symbol, price) from a decoded Webull quote, defensively — the SDK's
    decoded object's field names aren't documented, so try the common ones. Price
    preference: mid(bid,ask) → last/deal → close."""
    def g(*names):
        for n in names:
            v = getattr(msg, n, None)
            if v is None and isinstance(msg, dict):
                v = msg.get(n)
            if v not in (None, "", 0, "0"):
                return v
        return None

    sym = g("symbol", "ticker", "tickerSymbol", "disSymbol")
    bid = g("bidPrice", "bid", "bidPrice1")
    ask = g("askPrice", "ask", "askPrice1")
    last = g("price", "deal", "lastPrice", "tradePrice", "close", "pPrice")
    try:
        if bid and ask:
            px = (Decimal(str(bid)) + Decimal(str(ask))) / Decimal(2)
        elif last:
            px = Decimal(str(last))
        elif ask or bid:
            px = Decimal(str(ask or bid))
        else:
            px = None
    except (InvalidOperation, ValueError):
        px = None
    return (str(sym) if sym else None), (px if px and px > 0 else None)


def _on_quotes_message(*args: Any) -> None:
    """Webull MQTT callback. Signature is SDK-defined; accept *args and find the
    decoded quote among them. Logs the first message so we can confirm the shape
    against live keys, then extracts symbol+price into the shared cache."""
    global _logged_sample
    try:
        payload = args[-1] if args else None
        if not _logged_sample:
            _logged_sample = True
            log.info("webull_market_stream: first message args=%s payload=%r",
                     len(args), repr(payload)[:400])
        sym, px = _extract(payload)
        if sym and px is not None:
            _set_price(sym, px)
    except Exception:  # noqa: BLE001
        log.exception("webull_market_stream: message handler failed")


def _run_stream(symbols: frozenset[str], generation: int) -> bool:
    """Build the DataStreamingClient, subscribe to ``symbols`` (US stocks) and
    start the MQTT loop (non-blocking — paho runs on its own thread). Waits
    briefly and returns whether the socket actually came up, so the supervisor
    can back off on a persistent failure (bad/entitlement-less keys)."""
    global _client
    from webull.data.common.category import Category  # noqa: PLC0415
    from webull.data.data_streaming_client import DataStreamingClient  # noqa: PLC0415
    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    sub_types = [t.strip() for t in (s.webull_data_sub_types or "QUOTE").split(",") if t.strip()]
    client = DataStreamingClient(
        s.webull_data_app_key, s.webull_data_app_secret,
        s.webull_data_region_id, uuid.uuid4().hex,
    )
    # The SDK's connect flow writes a log file to cwd (/app), which is a READ-ONLY
    # filesystem under our container hardening — that OSError crashes the MQTT
    # loop thread (the connect itself succeeds). No-op its file logger; we have
    # our own logging via _on_quotes_message. (The gRPC listener sidesteps the
    # same issue by pre-marking logger flags.)
    try:
        client.set_file_logger = lambda *a, **k: None  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        pass
    client.on_quotes_message = _on_quotes_message

    def _on_connected(*_a: Any) -> None:
        if generation != _generation:
            return
        try:
            client.subscribe(list(symbols), Category.US_STOCK, sub_types)
            log.info("webull_market_stream: subscribed %d US stocks (sub_types=%s)",
                     len(symbols), sub_types)
        except Exception:  # noqa: BLE001
            log.exception("webull_market_stream: subscribe failed")

    client.on_connect_success = _on_connected
    _client = client
    client.connect_and_loop_start()
    log.info("webull_market_stream: connecting (%d symbols)", len(symbols))
    # Give the async connect a moment, then report liveness. On bad creds the SDK
    # raises 401 on its loop thread and never connects, so is_connected() stays
    # False and the supervisor backs off.
    time.sleep(5)
    return _is_connected()


def _stop_stream() -> None:
    global _client
    c = _client
    _client = None
    if c is not None:
        try:
            c.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def _supervise() -> None:
    global _current_symbols, _generation, _fail_streak
    while True:
        interval = _REFRESH_S
        try:
            if _enabled():
                symbols = frozenset(await asyncio.to_thread(_compute_symbols))
                # Restart when: symbols changed, no client, OR the client died
                # (the liveness check — the SDK won't auto-reconnect on its own).
                need_restart = bool(symbols) and (
                    symbols != _current_symbols or _client is None or not _is_connected()
                )
                if need_restart:
                    _stop_stream()
                    _generation += 1
                    _current_symbols = symbols
                    ok = await asyncio.to_thread(_run_stream, symbols, _generation)
                    if ok:
                        _fail_streak = 0
                    else:
                        _fail_streak += 1
                        interval = min(_BACKOFF_MAX, _REFRESH_S * (2 ** min(_fail_streak, 3)))
                        # Log once per streak start and occasionally after, not every tick.
                        if _fail_streak == 1 or _fail_streak % 5 == 0:
                            log.warning(
                                "webull_market_stream: not connected (attempt %d) — "
                                "check creds/entitlement; backing off to %.0fs",
                                _fail_streak, interval,
                            )
                elif not symbols:
                    _stop_stream()
                    _current_symbols = frozenset()
                    _fail_streak = 0
                else:
                    _fail_streak = 0  # healthy and unchanged
            else:
                _stop_stream()
                _current_symbols = frozenset()
                _fail_streak = 0
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("webull_market_stream supervisor pass failed")
        await asyncio.sleep(interval)


def start_webull_market_stream() -> None:
    """Spawn the supervisor. Idempotent, worker-only. No-op while disabled."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("webull_market_stream: no running loop; not starting")
        return
    _task = loop.create_task(_supervise())
    log.info("webull_market_stream: supervisor started (enabled=%s)", _enabled())


async def stop_webull_market_stream() -> None:
    global _task
    _stop_stream()
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None


__all__ = ["start_webull_market_stream", "stop_webull_market_stream"]
