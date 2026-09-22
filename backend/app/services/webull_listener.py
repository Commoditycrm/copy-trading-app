"""Direct-Webull real-time trade listener (gRPC).

Streams a master trader's order events from Webull's OpenAPI over gRPC (~0.2s
from fill to us, vs SnapTrade's minutes) and — once out of shadow mode — hands
each new order to ``copy_engine.fanout_threadsafe`` exactly like
``snaptrade_listener`` does. This module is the trader-side DETECTION signal
only; where a subscriber's mirror is EXECUTED is independent of it (a subscriber
on direct Webull executes through ``app.brokers.webull``, with fills synced by
``services.webull_subscriber_reconciler``).

Public interface mirrors ``snaptrade_listener`` / ``trade_listener`` so
``services.listeners`` can drive it identically: ``bind_loop``,
``start_all_listeners``, ``start_listener``, ``stop_listener``,
``stop_all_listeners``, ``has_running_listener``, ``running_trader_ids``,
and a ``_tasks`` registry.

Gating (all default to the SAFE state — nothing runs until explicitly enabled):
  * ``settings.webull_direct_enabled``   — master switch (default False).
  * ``settings.webull_direct_shadow_mode`` — when True (default) the listener
    DETECTS + logs the trader's orders but does NOT persist or fan out, so we
    can verify parity against SnapTrade before trusting real mirrors.

The Webull SDK is imported LAZILY inside ``_build_stoppable_client`` — importing
this module never requires the SDK to be installed.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select, text

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import (
    InstrumentType,
    OptionRight,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.models.user import User, UserRole
from app.services import listener_state, order_intent
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

# The Webull SDK's HTTP initializer logs "_check_token_enable result is False"
# at INFO on every REST client build — with a 5s poll that floods the log.
# Silence it to WARNING; our own webull-listener/webull-poll logs are untouched.
logging.getLogger("webull.core.http.initializer.client_initializer").setLevel(logging.WARNING)

# ── module state (same shape as the other listeners) ────────────────────────
_main_loop: asyncio.AbstractEventLoop | None = None
_tasks: dict[uuid.UUID, asyncio.Task] = {}
# The live stoppable gRPC client per trader — do_subscribe() blocks in a thread
# and an asyncio cancel can't interrupt that thread, so we keep the client to
# call request_stop() (closes the channel + breaks the retry loop).
_clients: dict[uuid.UUID, Any] = {}
# Monotonic generation per trader. A callback captures the generation it was
# created with and drops events if a newer listener has since started for that
# trader — so a lingering thread from a replaced client can never fan out.
_generation: dict[uuid.UUID, int] = {}

# ── REST poll backstop (per trader) ──────────────────────────────────────────
# The gRPC stream needs a per-app_key push scope that Webull doesn't always
# enable, so we ALSO poll the REST order API and feed the same handler. Both
# paths dedup by broker_order_id in _persist_and_fanout, so running them
# together never double-fires. State is keyed by trader like the stream:
_poll_tasks: dict[uuid.UUID, asyncio.Task] = {}
# order_ids that already existed when the poller started and that we decided are
# HISTORY — never to be replayed as fresh signals. See _build_poll_baseline for
# what does and does not land in here; it is deliberately narrower than "every
# order visible on the first cycle".
_poll_baseline: dict[uuid.UUID, set[str]] = {}
# order_id → last status we acted on (post-baseline), so we only process a
# genuine new order or a status transition (submit → fill → cancel), not the
# same unchanged row every cycle.
_poll_status: dict[uuid.UUID, dict[str, str]] = {}

# How far back an UNSEEN order can have been placed and still be treated as live
# rather than history when the poller starts. Sized for a deploy/restart: the
# worker is down for seconds, and a trade placed in that gap should still reach
# subscribers. Anything older is genuinely earlier-in-day activity — replaying it
# would mirror trades whose price has long moved.
_POLL_CATCHUP_WINDOW_S = 180.0

_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0

get_status = listener_state.get_status
_set_state = listener_state.set_state


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def _enabled() -> bool:
    from app.config import get_settings  # noqa: PLC0415
    return bool(get_settings().webull_direct_enabled)


def _shadow() -> bool:
    from app.config import get_settings  # noqa: PLC0415
    return bool(get_settings().webull_direct_shadow_mode)


def _poll_enabled() -> bool:
    from app.config import get_settings  # noqa: PLC0415
    return bool(get_settings().webull_direct_poll_enabled)


# Webull's published limits (developer.webull.com/apis/docs/rate-limits,
# Production column) for the ORDER-QUERY endpoints this poll uses:
#
#   Order History / Open Orders / Order Detail   2/2s
#   Account Positions / Account Balance          2/2s
#   Place / Replace / Cancel Order             600/60s
#
# Two things here were previously wrong, and both made the poller slower than it
# needs to be. The limits are NOT "10/30s shared across every endpoint": the docs
# state each endpoint keeps its OWN counter ("Hitting the limit on one endpoint
# does not affect others"), and a query endpoint allows 1 call/s sustained —
# three times the 20/min we assumed. Reads also cannot starve order placement,
# which is on a separate and far larger counter.
#
# What IS real is the BURST shape: 2/2s is a two-second window, so back-to-back
# calls are what trips it, not the average rate. That is why the per-account
# calls are still spaced across the cycle rather than fired together (prod, 3
# accounts, 2026-08-13: the 3rd call of each burst 429'd 32x/hr). Spacing just
# over 1s per call holds the sustained rate at the cap with headroom.
#
# NOTE: those figures document the /trading/... endpoints, while this SDK still
# calls the older /openapi/... paths, so the margin below is deliberate rather
# than tuned to the published ceiling.
_DAYORDERS_MIN_INTERVAL_S = 1.2          # per-call floor (2/2s ⇒ 1/s, +20% margin)
_DAYORDERS_PER_ACCOUNT_S = 1.2           # spacing between accounts in one cycle


def _poll_interval() -> float:
    """Configured base interval, floored to the single-account rate limit."""
    from app.config import get_settings  # noqa: PLC0415
    return max(_DAYORDERS_MIN_INTERVAL_S, float(get_settings().webull_poll_interval_seconds))


def _safe_poll_interval(num_accounts: int) -> float:
    """Effective interval for the rate limit: max(configured, 3.3s × accounts).
    One list_today_orders call per account per cycle all draw on the SAME
    10-req/30s app-id budget, so more accounts ⇒ a longer cycle."""
    return max(_poll_interval(), _DAYORDERS_PER_ACCOUNT_S * max(1, num_accounts))


# ── credentials ─────────────────────────────────────────────────────────────
def _all_account_ids(creds: dict[str, Any]) -> list[str]:
    """All of the trader's Webull account_ids. A trader often has several
    accounts (Cash / Margin / …) under one login and may trade on any of
    them — Webull's stream only pushes events for the accounts you subscribe
    to, so we subscribe to ALL of them (the connected one is the fallback if
    the lookup fails). Matches the localhost script that streamed correctly."""
    try:
        t = _webull_trade_client(creds)  # file logger suppressed inside
        res = t.account_v2.get_account_list()
        if getattr(res, "status_code", None) == 200:
            ids = [str(a.get("account_id")) for a in (res.json() or []) if isinstance(a, dict) and a.get("account_id")]
            if ids:
                return ids
    except Exception:  # noqa: BLE001
        log.warning("webull-listener: get_account_list failed; subscribing to configured account only", exc_info=True)
    return [creds["account_id"]]


def _load_creds(broker_account_id: uuid.UUID) -> dict[str, Any] | None:
    with SessionLocal() as db:
        acct = db.get(BrokerAccount, broker_account_id)
        if acct is None or acct.connection_status != "connected":
            return None
        try:
            return decrypt_json(acct.encrypted_credentials)
        except Exception:  # noqa: BLE001
            log.exception("webull-listener: decrypt creds failed for %s", broker_account_id)
            return None


# ── stoppable gRPC events client (lazy SDK import) ──────────────────────────
def _build_stoppable_client(creds: dict[str, Any]):
    """Subclass Webull's TradeEventsClient so we can stop it cleanly: a custom
    retry policy that returns NO_RETRY once a stop flag is set, plus a
    do_subscribe override that stores the channel so request_stop() can close
    it (which raises inside the stream loop → retry check → NO_RETRY → return)."""
    import grpc  # noqa: PLC0415
    import webull.trade.events.events_pb2_grpc as pb_grpc  # noqa: PLC0415
    from webull.core.retry.retry_condition import RetryCondition  # noqa: PLC0415
    from webull.trade.events.default_retry_policy import (  # noqa: PLC0415
        DefaultSubscribeRetryPolicy,
    )
    from webull.trade.trade_events_client import TradeEventsClient  # noqa: PLC0415

    class _StopAwareRetryPolicy(DefaultSubscribeRetryPolicy):
        def __init__(self, stop_event: threading.Event):
            super().__init__()
            self._stop_event = stop_event

        def should_retry(self, ctx):
            if self._stop_event.is_set():
                return RetryCondition.NO_RETRY
            return super().should_retry(ctx)

    class _StoppableTradeEvents(TradeEventsClient):
        def __init__(self, app_key, app_secret, region_id):
            self._stop_event = threading.Event()
            super().__init__(app_key, app_secret, region_id,
                             retry_policy=_StopAwareRetryPolicy(self._stop_event))
            self._grpc_channel = None

        def request_stop(self) -> None:
            self._stop_event.set()
            ch = self._grpc_channel
            if ch is not None:
                try:
                    ch.close()
                except Exception:  # noqa: BLE001
                    pass

        def _build_request(self, app_key, app_secret, accounts):  # noqa: D401 — override
            # Webull Support (ticket, 2026-08): the ONLY supported subscribeType
            # is 1. The bundled SDK hardcodes 7 (with a now-stale "1 2 4 allowed"
            # comment); the gRPC endpoint rejects that, so NO trade events ever
            # arrive. Rebuild the request with subscribeType=1 — everything else
            # mirrors the SDK's _build_request (sign → metadata), minus its debug
            # prints. Ref: developer.webull.com/apis/docs/reference/custom/subscribe-trade-events
            import time as _time  # noqa: PLC0415
            import webull.trade.events.events_pb2 as _pb  # noqa: PLC0415
            from webull.core.auth.algorithm import sha_hmac256_new  # noqa: PLC0415
            from webull.trade.events.signature_composer import calc_signature  # noqa: PLC0415
            request = _pb.SubscribeRequest(
                subscribeType=1,
                timestamp=int(_time.time() * 1000),  # millis
                accounts=accounts,
            )
            _sig, metadata = calc_signature(app_key, app_secret, request, sha_hmac256_new)
            return request, metadata

        def do_subscribe(self, accounts):  # noqa: D401 — override
            target = f"{self._host}:{self._port}"
            if self._tls_enable:
                channel = grpc.secure_channel(target, grpc.ssl_channel_credentials())
            else:
                channel = grpc.insecure_channel(target)
            self._grpc_channel = channel
            try:
                if self._stop_event.is_set():
                    return
                stub = pb_grpc.EventServiceStub(channel)
                self._stream_processing(stub, accounts)
            finally:
                try:
                    channel.close()
                except Exception:  # noqa: BLE001
                    pass
                self._grpc_channel = None

    return _StoppableTradeEvents(creds["app_key"], creds["app_secret"], creds.get("region_id", "us"))


# ── order mapping + option resolution (Stage 3 live path) ───────────────────
_WEBULL_STATUS: dict[str, OrderStatus] = {
    "FILLED": OrderStatus.FILLED,
    "PARTIAL_FILLED": OrderStatus.PARTIALLY_FILLED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "PENDING": OrderStatus.SUBMITTED,
    "PENDING_SUBMIT": OrderStatus.SUBMITTED,
    "SUBMITTED": OrderStatus.SUBMITTED,
    "WORKING": OrderStatus.ACCEPTED,
    "ACCEPTED": OrderStatus.ACCEPTED,
    "QUEUED": OrderStatus.ACCEPTED,
    "CANCELLED": OrderStatus.CANCELED,
    "CANCELED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "FAILED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}
_OPEN_OR_FILLED = (
    OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
)
_WORKING = (OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)


def _dec(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _advisory_key(trader_user_id: uuid.UUID, broker_order_id: str) -> int:
    """Stable signed 64-bit key for pg_advisory_xact_lock, derived from
    (trader, broker_order_id). Two handlers for the SAME broker order hash to
    the same key and serialize; different orders don't contend. blake2b (not
    Python's salted hash()) so the value is identical across processes/threads.
    Matches trade_listener._advisory_key."""
    digest = hashlib.blake2b(
        f"{trader_user_id}:{broker_order_id}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


def _parse_wb_time(v: Any) -> datetime | None:
    """Parse a Webull timestamp to an aware datetime. Handles both the stream's
    ISO form (``2026-08-06T14:04:46.424+0000``) and the REST form with a space
    separator (``2026-08-06 16:47:17.816+0000``), normalising ``Z`` and the
    ``+0000`` (no-colon) offset that ``fromisoformat`` rejects. None on failure."""
    if not isinstance(v, str) or not v:
        return None
    s = v.strip().replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# Terminal statuses → stamp closed_at (matches snaptrade_listener).
_TERMINAL = (
    OrderStatus.FILLED, OrderStatus.CANCELED,
    OrderStatus.REJECTED, OrderStatus.EXPIRED,
)


def _map_status(s: str | None) -> OrderStatus:
    return _WEBULL_STATUS.get(str(s or "").upper(), OrderStatus.SUBMITTED)


def _map_side(s: str | None) -> OrderSide:
    return OrderSide.SELL if str(s or "").upper() == "SELL" else OrderSide.BUY


def _map_order_type(s: str | None) -> OrderType:
    """Webull's order-type name -> ours. MUST round-trip WebullAdapter's
    _ORDER_TYPE_MAP, which sends STOP as "STOP_LOSS".

    "STOP_LOSS" was missing while "STOP_LOSS_LIMIT" was handled, so every plain
    stop fell through to MARKET. The modify branch then saw the order's type as
    changed and rewrote our own row: a resting protective STOP was stored, and
    shown, as a MARKET order -- on the very rows where being able to tell those
    apart matters most. Anything unrecognised still falls back to MARKET.
    """
    t = str(s or "").upper()
    if t in ("LIMIT", "LMT"):
        return OrderType.LIMIT
    if t in ("STOP", "STP", "STOP_LOSS"):
        return OrderType.STOP
    if t in ("STOP_LIMIT", "STP_LMT", "STOP_LOSS_LIMIT"):
        return OrderType.STOP_LIMIT
    if t in ("TRAILING_STOP", "TRAILING_STOP_LOSS"):
        return OrderType.TRAILING_STOP
    return OrderType.MARKET


# ── cached REST client (per app_key) ─────────────────────────────────────────
# CRITICAL: TradeClient.__init__ runs the SDK's token flow (init_token →
# fetch_token_from_server, a network call to Webull's auth endpoint). Building a
# fresh client on EVERY poll cycle (every 5s) hammered that endpoint → 429s,
# repeated 2FA challenges, and eventually a VERIFY_FAILURE_EXCEED_LIMIT lockout
# that stopped order detection entirely. So ONE client per app_key is built and
# reused; the token flow then runs once per TTL, not every poll.
#
# The cache lives in app.brokers.webull and is SHARED with the adapter. This
# module used to keep an identical one of its own, which meant a trader's
# app_key ran the token flow twice per TTL — pure extra load on the very
# endpoint whose rate limit caused the lockout above — and left two places to
# reason about auth.


def _webull_trade_client(creds: dict[str, Any]):
    from app.brokers.webull import trade_client_for  # noqa: PLC0415
    return trade_client_for(
        creds["app_key"], creds["app_secret"], creds.get("region_id", "us"),
    )


def _invalidate_trade_client(creds: dict[str, Any]) -> None:
    """Drop the cached client so the next call rebuilds it (re-auths). Call only
    on AUTH failures — NOT on 429s (a 429 means throttled, not bad auth;
    rebuilding would re-hit the token endpoint and make throttling worse)."""
    from app.brokers.webull import invalidate_trade_client  # noqa: PLC0415
    invalidate_trade_client(creds.get("app_key"))


def _resolve_option_contract(
    creds: dict[str, Any], account_id: str, client_order_id: str,
) -> tuple[Decimal, date, OptionRight] | None:
    """Resolve an option order's (strike, expiry, right) from Webull.

    The trade-event payload carries only symbol + instrument_id + category, not
    the contract terms. We fetch the order detail (which echoes the option leg
    the order was placed with) and parse strike_price / option_expire_date /
    option_type. Returns None if it can't be resolved — the caller then REFUSES
    to mirror the option (never mirror a wrong contract).

    NOTE: the exact response field names must be validated against a real Webull
    option order before enabling live option mirroring (flip shadow off)."""
    try:
        trade = _webull_trade_client(creds)
        res = trade.order_v2.get_order_detail(account_id, client_order_id)
        if getattr(res, "status_code", None) != 200:
            return None
        body = res.json() or {}
    except Exception:  # noqa: BLE001
        log.warning("webull-listener: option resolve failed for %s", client_order_id, exc_info=True)
        return None

    candidates: list[dict] = []
    if isinstance(body, list):
        candidates = [x for x in body if isinstance(x, dict)]
    elif isinstance(body, dict):
        candidates = [body]
        for key in ("legs", "orders", "items", "order_legs"):
            v = body.get(key)
            if isinstance(v, list):
                candidates += [x for x in v if isinstance(x, dict)]

    for c in candidates:
        legs = c.get("legs") if isinstance(c.get("legs"), list) else [c]
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            strike = _dec(leg.get("strike_price") or leg.get("strike"))
            exp_raw = (leg.get("option_expire_date") or leg.get("expiration_date")
                       or leg.get("expire_date"))
            rt = str(leg.get("option_type") or leg.get("option_right") or "").upper()
            if strike is None or not exp_raw:
                continue
            try:
                expiry = date.fromisoformat(str(exp_raw)[:10])
            except ValueError:
                continue
            right = (OptionRight.CALL if rt.startswith("C")
                     else OptionRight.PUT if rt.startswith("P") else None)
            if right is None:
                continue
            return (strike, expiry, right)
    return None


def _persist_and_fanout(
    trader_user_id: uuid.UUID, broker_account_id: uuid.UUID,
    creds: dict[str, Any], payload: dict,
) -> None:
    """Live path (shadow OFF only): persist the trader's Webull order and hand
    NEW ones to the fanout — mirrors snaptrade_listener._persist_and_fanout
    (dedup by broker_order_id, subscriber-skip, fanout_threadsafe)."""
    from app.services import audit, broker_filters, copy_engine, discord_alerts, events  # noqa: PLC0415

    broker_order_id = str(payload.get("order_id") or "").strip()
    if not broker_order_id:
        return
    status_enum = _map_status(payload.get("order_status"))
    is_option = str(payload.get("category") or "").upper() == "US_OPTION"

    with SessionLocal() as db:
        # Serialize concurrent handling of the SAME broker order so the
        # check-then-insert below can't race into two parent rows (the
        # "doubling" bug — two rows, same broker_order_id, ~ms apart). Covers a
        # brief overlap of two poller generations during a listener restart, or
        # the poll + stream paths landing together. Held until this transaction
        # commits; a second handler then sees the committed row and takes the
        # UPDATE path instead of inserting. Same guard as trade_listener.
        db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": _advisory_key(trader_user_id, broker_order_id)},
        )
        # Respect the per-account listener toggles (Auto Pull / Bring open /
        # Bring filled), same as snaptrade_listener.
        acct_gate = db.get(BrokerAccount, broker_account_id)
        if not broker_filters.should_persist_order(acct_gate, status_enum):
            return

        existing = find_placed_order(
            db, trader_user_id,
            broker_order_id, str(payload.get("client_order_id") or "").strip(),
        )

        if existing is not None:
            was_working = existing.status in _WORKING
            # Did THIS event flip the order to FILLED? Used to fire the Discord
            # alert exactly once on the fill transition (not on later quiescent
            # updates for an already-filled order, and not on a restart re-poll).
            became_filled = (
                existing.status != OrderStatus.FILLED
                and status_enum == OrderStatus.FILLED
            )

            # ── Trader MODIFY: still-working terms changed → propagate as a
            # cancel-replace onto the mirrors (only when the event carries the
            # working terms; a plain fill event won't trip this). ──
            if was_working and status_enum in (OrderStatus.SUBMITTED, OrderStatus.ACCEPTED):
                new_qty = _dec(payload.get("qty"))
                new_type = _map_order_type(payload.get("order_type"))
                new_limit = _dec(payload.get("limit_price"))
                new_stop = _dec(payload.get("stop_price"))
                modified = (
                    (new_qty is not None and existing.quantity != new_qty)
                    or existing.order_type != new_type
                    or (new_limit is not None and existing.limit_price != new_limit)
                    or (new_stop is not None and existing.stop_price != new_stop)
                )
                if modified:
                    if new_qty is not None:
                        existing.quantity = new_qty
                    existing.order_type = new_type
                    if new_limit is not None:
                        existing.limit_price = new_limit
                    if new_stop is not None:
                        existing.stop_price = new_stop
                    db.commit()
                    db.refresh(existing)
                    # Push the modified terms to the trader's UI (upsert) so the
                    # qty/price change reflects live without a refresh.
                    events.publish(
                        trader_user_id,
                        copy_engine._order_event("order.placed", existing),  # noqa: SLF001
                    )
                    try:
                        copy_engine.propagate_modify_to_mirrors(existing.id)
                    except Exception:  # noqa: BLE001
                        log.exception("webull-listener modify propagate failed for %s", existing.id)
                    return

            # ── Normal status / fill update ──
            if existing.status != status_enum:
                existing.status = status_enum
            fq = _dec(payload.get("filled_qty"))
            if fq is not None:
                existing.filled_quantity = fq
            fp = _dec(payload.get("filled_price"))
            if fp is not None:
                existing.filled_avg_price = fp
            # Stamp the close time once the order terminalizes (fill/cancel/…),
            # so Order History shows a completed date instead of a blank.
            if status_enum in _TERMINAL and existing.closed_at is None:
                existing.closed_at = _parse_wb_time(payload.get("filled_time")) or datetime.now(timezone.utc)
            if existing.socket_received_at is None:
                existing.socket_received_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(existing)

            # Push the status change (fill / cancel / …) to the trader's UI as an
            # upsert so Order History reflects it live — without this the row only
            # updates on a manual refresh. Matches snaptrade_listener's update pass.
            events.publish(
                trader_user_id,
                copy_engine._order_event("order.placed", existing),  # noqa: SLF001
            )

            # Broadcast the trader's fill to their Discord channel on the
            # working→filled transition (fire-and-forget; gated + deduped inside).
            if became_filled and existing.parent_order_id is None:
                try:
                    discord_alerts.emit_trader_fill_alert(existing.id)
                except Exception:  # noqa: BLE001
                    log.exception("webull-listener discord alert failed for %s", existing.id)

            # ── Trader CANCEL → cascade-cancel the subscriber mirrors ──
            if was_working and status_enum == OrderStatus.CANCELED:
                from app.api.trades import _run_cancel_fanout_in_background  # noqa: PLC0415
                try:
                    _run_cancel_fanout_in_background(existing.id)
                except Exception:  # noqa: BLE001
                    log.exception("webull-listener cancel-cascade failed for %s", existing.id)
            return

        if status_enum not in _OPEN_OR_FILLED:
            return

        # Don't re-create an order our OWN app placed. api/trades.py marks the
        # Order id as app-originated and we pass that id to Webull as the
        # client_order_id, so it comes back on every event here.
        #
        # Without this the dedup above cannot work at all on Webull: we store
        # OUR client_order_id as broker_order_id (see WebullAdapter.place_order),
        # while this listener looks the order up by WEBULL's order_id. Two
        # different identifiers for one order, so the SELECT always misses and
        # every app-placed order is inserted a SECOND time — then fanned out
        # again at the bottom of this function, giving each subscriber TWO
        # mirrors for one trader trade (prod: 4 confirmed double-mirrors).
        #
        # The duplicate also loses the contract: this path rebuilds the order
        # from the feed payload, and when that payload doesn't identify it as an
        # option the row is typed STOCK with strike/expiry/right NULL. Realized
        # P&L then multiplies by 1 instead of 100 — an $86 trade displayed as
        # $0.86 — and the two rows land in different FIFO buckets so the
        # round-trip may never match at all.
        #
        # trade_listener has had this guard since the Alpaca "doubling" bug; it
        # was never carried across to the other listeners.
        _coid = payload.get("client_order_id")
        if _coid:
            try:
                # Webull caps client_order_id at 32 chars, so we send the Order
                # UUID with its dashes stripped — parse the hex form back.
                _app_oid = uuid.UUID(hex=str(_coid).strip())
            except (ValueError, TypeError, AttributeError):
                _app_oid = None
            if _app_oid is not None and order_intent.is_app_originated(_app_oid):
                log.info(
                    "webull-listener[%s] skipping app-originated order "
                    "(client_order_id=%s, webull order_id=%s) — the Trade Panel "
                    "owns this order's row and fanout",
                    trader_user_id, _coid, broker_order_id,
                )
                return

        owner = db.get(User, trader_user_id)
        if owner is not None and owner.role == UserRole.SUBSCRIBER:
            return

        symbol = str(payload.get("symbol") or "").upper()
        option_expiry = option_strike = option_right = None
        if is_option:
            resolved = _resolve_option_contract(
                creds, str(payload.get("account_id")), str(payload.get("client_order_id")),
            )
            if resolved is None:
                log.error(
                    "webull-listener[%s] REFUSING to mirror option %s (%s) — could not "
                    "resolve strike/expiry/right; order NOT fanned out.",
                    trader_user_id, symbol, broker_order_id,
                )
                return
            option_strike, option_expiry, option_right = resolved

        qty = _dec(payload.get("qty")) or Decimal(0)
        if qty <= 0:
            return

        now = datetime.now(timezone.utc)
        # Placement time from Webull; fall back to fill time, then now — so the
        # Order History "submitted" column and Performance latency chain are
        # populated exactly like the SnapTrade/Alpaca path (never blank).
        placed_at = _parse_wb_time(payload.get("place_time")) or _parse_wb_time(payload.get("filled_time"))
        closed_at = (
            (_parse_wb_time(payload.get("filled_time")) or now)
            if status_enum in _TERMINAL else None
        )

        order = Order(
            id=uuid.uuid4(),
            user_id=trader_user_id,
            broker_account_id=broker_account_id,
            instrument_type=InstrumentType.OPTION if is_option else InstrumentType.STOCK,
            symbol=symbol,
            option_expiry=option_expiry,
            option_strike=option_strike,
            option_right=option_right,
            side=_map_side(payload.get("side")),
            order_type=_map_order_type(payload.get("order_type")),
            quantity=qty,
            limit_price=_dec(payload.get("limit_price")),
            stop_price=_dec(payload.get("stop_price")),
            is_closing=False,   # fanout detects close per-subscriber from held qty
            status=status_enum,
            filled_quantity=_dec(payload.get("filled_qty")) or Decimal(0),
            filled_avg_price=_dec(payload.get("filled_price")),
            broker_order_id=broker_order_id,
            submitted_at=placed_at or now,
            trader_submitted_at=placed_at,
            closed_at=closed_at,
            socket_received_at=now,
        )

        db.add(order)
        audit.record(
            db, actor_user_id=trader_user_id, action="listener.order_observed",
            entity_type="order", entity_id=order.id,
            metadata={"broker": "webull", "broker_order_id": broker_order_id,
                      "status": str(payload.get("order_status")), "symbol": symbol,
                      "side": order.side.value, "qty": str(order.quantity)},
        )
        db.commit()
        db.refresh(order)

        events.publish(trader_user_id, copy_engine._order_event("order.placed", order))  # noqa: SLF001

        if _main_loop is not None:
            copy_engine.fanout_threadsafe(order.id, trader_user_id, _main_loop)
        else:
            trader = db.get(User, trader_user_id)
            if trader is not None:
                copy_engine.fanout(db, order, trader)
                order.fanned_out_to_subscribers = True
                db.commit()
        # NOTE: a first-seen FILLED order is broadcast by copy_engine.fanout_async
        # (the single detection point for every broker), so we do NOT emit here —
        # doing so would race a second thread against the same dedup marker.


# ── REST poll backstop ───────────────────────────────────────────────────────
# Webull caps this endpoint at 100 per page. 30 was enough for a normal day and
# silently was not for a busy one: a single account placed 47 orders in an
# afternoon of testing, so anything past the page's edge was never seen by the
# poller at all — its fills and cancels simply never landed in the order
# history. A bigger page costs the SAME one request, so there is no rate-limit
# reason to keep it small.
_DAYORDERS_PAGE_SIZE = 100


def _list_today_orders(
    creds: dict[str, Any], account_id: str, page_size: int = _DAYORDERS_PAGE_SIZE,
) -> list[dict]:
    """One page of the account's orders for today. Returns [] on
    any failure (the poller just tries again next cycle). Response shape:
    ``{"hasNext":..., "pageSize":..., "orders":[{order_id, client_order_id,
    account_id, items:[{symbol, category, side, order_status, qty, filled_qty,
    filled_price, last_filled_time, order_type, limit_price, ...}], ...}]}``."""
    try:
        t = _webull_trade_client(creds)
        res = t.order.list_today_orders(account_id, page_size=page_size)
        if getattr(res, "status_code", None) != 200:
            log.warning("webull-poll: list_today_orders http %s for %s",
                        getattr(res, "status_code", "?"), account_id)
            return []
        body = res.json() or {}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        log.warning("webull-poll: list_today_orders failed for %s: %s", account_id, msg[:160])
        # A 429/throttle is transient — KEEP the cached client (rebuilding would
        # re-run the token flow and pile more load on Webull's auth endpoint,
        # exactly what caused the lockout). Rebuild only on a genuine auth error.
        if not ("TOO_MANY" in msg or "429" in msg or "throttl" in msg.lower()):
            _invalidate_trade_client(creds)
        return []
    if isinstance(body, list):
        return [o for o in body if isinstance(o, dict)]
    rows = body.get("orders") or body.get("items") or body.get("data") or []
    return [o for o in rows if isinstance(o, dict)]


def _order_fingerprint(payload: dict) -> str:
    """A compact signature of an order's MUTABLE fields. The poller reprocesses
    a row whenever this changes — so it catches not just status transitions
    (working → filled/canceled) but MODIFIES too (qty/price/type change while
    the status stays 'working'), and incremental fills (filled_qty rising within
    PARTIALLY_FILLED). Keyed only on fields that can legitimately change."""
    return "|".join(str(payload.get(k) or "") for k in (
        "order_status", "order_type", "qty", "limit_price", "stop_price",
        "filled_qty", "filled_price",
    ))


def _rest_order_to_payload(o: dict) -> dict | None:
    """Flatten a REST today-orders row into the SAME payload dict the gRPC
    handler consumes, so the poll and stream share _on_order_event /
    _persist_and_fanout unchanged. The leg detail lives in items[0]; order_id /
    client_order_id / account_id are on the wrapper."""
    if not isinstance(o, dict):
        return None
    oid = str(o.get("order_id") or "").strip()
    if not oid:
        return None
    items = o.get("items") or []
    leg = items[0] if items and isinstance(items[0], dict) else {}
    # REST fill time uses "YYYY-MM-DD HH:MM:SS.mmm+0000" (space) — normalise the
    # separator to 'T' so the existing ISO parser in _persist_and_fanout works.
    ft = leg.get("last_filled_time") or leg.get("filled_time") or o.get("last_filled_time")
    if isinstance(ft, str) and " " in ft and "T" not in ft:
        ft = ft.replace(" ", "T", 1)
    return {
        "order_id": oid,
        "client_order_id": o.get("client_order_id"),
        "account_id": o.get("account_id") or leg.get("account_id"),
        "order_status": leg.get("order_status") or o.get("order_status"),
        "category": leg.get("category") or o.get("combo_ticker_type"),
        "symbol": leg.get("symbol"),
        "side": leg.get("side"),
        "order_type": leg.get("order_type") or o.get("order_type"),
        "qty": leg.get("qty") or o.get("qty"),
        "filled_qty": leg.get("filled_qty"),
        "filled_price": leg.get("filled_price"),
        "filled_time": ft,
        "place_time": leg.get("place_time") or o.get("place_time"),
        "limit_price": leg.get("limit_price"),
        "stop_price": leg.get("stop_price"),
    }


def _build_poll_baseline(
    trader_user_id: uuid.UUID, orders: list[dict]
) -> tuple[set[str], dict[str, str]]:
    """Classify the orders visible on the poller's FIRST cycle.

    Returns ``(baseline, prefingerprints)``:
      * ``baseline``        — order_ids to treat as HISTORY and never replay;
      * ``prefingerprints`` — order_ids whose broker state already matches what
        we have stored, pre-seeded into the seen-map so this cycle does no work
        for them (a LATER change still flips the fingerprint and is processed).

    The poller restarts on every worker deploy, crash and listener reconcile —
    not just once a day — so "baseline everything currently on screen" was far
    too blunt. It silently dropped two classes of live work:

      1. An order we ALREADY have a row for that is still WORKING. Baselining it
         meant its later fill transition was never processed: the trader's limit
         filled, but ``force_fill_mirrors_to_market`` never fired, so every
         subscriber's mirror stayed a resting limit while the trader was out.
         These can never cause a spurious fanout — ``_persist_and_fanout`` finds
         the existing row and takes the UPDATE path — so they are never history.

      2. An order placed DURING the restart. The worker is typically down for
         seconds; a trade in that gap should still reach subscribers. Unseen
         orders placed within ``_POLL_CATCHUP_WINDOW_S`` are let through; older
         ones stay suppressed, because replaying a trade from hours ago would
         mirror it at a price that has long moved.

    Why the prefingerprints matter as much as the baseline: without them, every
    restart would re-run the handler over every known order. That is not just
    wasted commits — for a still-WORKING order, ``_persist_and_fanout``'s modify
    branch compares the payload's terms against the stored row and, on any
    difference, fires a cancel-and-replace across EVERY subscriber mirror. Seeding
    the fingerprint for orders whose status already agrees keeps the restart
    quiet while leaving genuinely-stale rows (filled while we were down) to be
    healed on this very cycle.

    Anything we cannot date is treated as history — the conservative reading, and
    the pre-existing behaviour.
    """
    ids = [str(o.get("order_id")) for o in orders if o.get("order_id")]
    if not ids:
        return set(), {}

    # What we already have for this trader: broker_order_id → our stored status.
    stored: dict[str, OrderStatus] = {}
    try:
        with SessionLocal() as db:
            for boid, status in db.execute(
                select(Order.broker_order_id, Order.status).where(
                    Order.user_id == trader_user_id,
                    Order.parent_order_id.is_(None),
                    Order.broker_order_id.in_(ids),
                )
            ).all():
                if boid:
                    stored[str(boid)] = status
    except Exception:  # noqa: BLE001
        # If we can't read our own history, suppress everything on screen — the
        # old behaviour, and the safe direction (a missed mirror beats replaying
        # a whole day of trades).
        log.exception(
            "webull-poll[%s] baseline: could not load known orders; "
            "treating all %d visible order(s) as history",
            trader_user_id, len(ids),
        )
        return set(ids), {}

    now = datetime.now(timezone.utc)
    baseline: set[str] = set()
    pre: dict[str, str] = {}
    in_sync = stale = caught_up = 0

    for o in orders:
        oid = str(o.get("order_id") or "")
        if not oid:
            continue
        payload = _rest_order_to_payload(o)

        if oid in stored:
            # (1) Already tracked. Never history. Quiet unless it moved on us.
            if payload is not None and _map_status(payload.get("order_status")) == stored[oid]:
                pre[oid] = _order_fingerprint(payload)
                in_sync += 1
            else:
                stale += 1          # broker moved while we were down → heal it now
            continue

        # (2) Never seen. Live only if it was placed during the restart gap.
        placed = _parse_wb_time((payload or {}).get("place_time"))
        if placed is not None and (now - placed).total_seconds() <= _POLL_CATCHUP_WINDOW_S:
            caught_up += 1
            continue
        baseline.add(oid)

    log.info(
        "webull-poll[%s] primed baseline: %d history / %d tracked-in-sync / "
        "%d tracked-stale (syncing now) / %d unseen within %.0fs (carried live)",
        trader_user_id, len(baseline), in_sync, stale, caught_up,
        _POLL_CATCHUP_WINDOW_S,
    )
    return baseline, pre


async def _run_poller(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    """Pull the trader's Webull orders on a short interval and hand any NEW
    order or status transition to _on_order_event (shadow/live routing + dedup
    are shared with the stream). Primes a baseline on the first cycle so the
    trader's earlier-in-day orders are never replayed as fresh signals."""
    if not _poll_enabled():
        return
    generation = _generation.get(trader_user_id, 0)
    _poll_baseline.pop(trader_user_id, None)
    _poll_status[trader_user_id] = {}

    # Resolve the account list once — accounts rarely change and get_account_list
    # every cycle would burn rate limit. Fall back to the configured account.
    creds0 = _load_creds(broker_account_id)
    account_ids: list[str] = (
        await asyncio.to_thread(_all_account_ids, creds0) if creds0 else []
    )
    interval = _safe_poll_interval(len(account_ids))
    log.info("webull-poll[%s] started; interval=%.1fs (%d account(s), 10-req/30s cap) accounts=%s",
             trader_user_id, interval, len(account_ids), account_ids)

    while True:
        try:
            if _generation.get(trader_user_id) != generation:
                return  # a newer listener/poller superseded us
            creds = _load_creds(broker_account_id)
            if creds is None or not creds.get("app_key"):
                await asyncio.sleep(30)
                continue
            if not account_ids:
                account_ids = await asyncio.to_thread(_all_account_ids, creds)
                interval = _safe_poll_interval(len(account_ids))

            # Space the per-account calls EVENLY across the cycle instead of
            # bursting them back-to-back. The query endpoints are limited 2/2s,
            # a two-second window — so a burst of N calls followed by one long
            # sleep trips it even when the AVERAGE rate is well under, and the
            # last account in the burst 429'd every cycle (prod, 3 accounts —
            # the 3rd 429'd 32×/hr, 2026-08-13). One call every interval/N keeps
            # the window under the cap. Per-account poll frequency is unchanged
            # (still once per `interval`), so detection latency is too.
            gap = interval / max(1, len(account_ids))
            orders: list[dict] = []
            for aid in account_ids:
                orders.extend(await asyncio.to_thread(_list_today_orders, creds, aid))
                await asyncio.sleep(gap)

            # First cycle: decide which of the orders already on screen are
            # HISTORY (never to be replayed) and which are live work we should
            # keep tracking. Everything not baselined falls through to the normal
            # per-order handling below on this very cycle.
            if trader_user_id not in _poll_baseline:
                _baseline, _pre = await asyncio.to_thread(
                    _build_poll_baseline, trader_user_id, orders,
                )
                _poll_baseline[trader_user_id] = _baseline
                # Orders already in sync with us start "seen", so this cycle
                # does no work for them; a later change still flips their
                # fingerprint and is processed normally. setdefault because
                # stop_listener can clear this map while the classify above is
                # still off-loop.
                _poll_status.setdefault(trader_user_id, {}).update(_pre)

            baseline = _poll_baseline[trader_user_id]
            seen = _poll_status[trader_user_id]
            # Oldest-first so a submit→fill sequence within one cycle applies in
            # order (the response is newest-first).
            for o in reversed(orders):
                oid = str(o.get("order_id") or "")
                if not oid or oid in baseline:
                    continue
                payload = _rest_order_to_payload(o)
                if payload is None:
                    continue
                fp = _order_fingerprint(payload)
                if seen.get(oid) == fp:
                    continue  # nothing mutable changed since we last acted on it
                seen[oid] = fp
                # Run off the loop — persist/fanout does blocking DB I/O.
                await asyncio.to_thread(
                    _on_order_event, trader_user_id, broker_account_id,
                    generation, creds, payload,
                )
            # No trailing sleep here — the per-account gaps above already paced
            # the full cycle (N × gap = interval).
            continue
        except asyncio.CancelledError:
            log.info("webull-poll[%s] cancelled", trader_user_id)
            raise
        except Exception:  # noqa: BLE001
            log.exception("webull-poll[%s] cycle failed", trader_user_id)
            # Error before/inside the paced loop: back off a full cycle so a
            # repeated failure can't hot-loop the endpoint.
            await asyncio.sleep(interval)


# ── event handling ──────────────────────────────────────────────────────────
def _on_order_event(
    trader_user_id: uuid.UUID, broker_account_id: uuid.UUID, generation: int,
    creds: dict[str, Any], payload: dict,
) -> None:
    """Runs in the gRPC callback thread. Shadow mode (default): log detection
    only — no DB writes, no fanout. Live mode: persist the trader order +
    fanout via ``_persist_and_fanout``."""
    if log.isEnabledFor(logging.DEBUG) and isinstance(payload, dict):
        log.debug(
            "webull-listener[%s] event %s %s status=%s boid=%s",
            trader_user_id, payload.get("symbol"), payload.get("side"),
            payload.get("order_status"), payload.get("order_id"),
        )
    # Drop events from a superseded listener (a lingering thread after restart).
    if _generation.get(trader_user_id) != generation:
        return
    if not isinstance(payload, dict):
        return

    if _shadow():
        # Detection-only: log the event + end-to-end latency, nothing else.
        lat = ""
        ft = payload.get("filled_time")
        try:
            if isinstance(ft, str) and ft:
                s = ft.strip().replace("Z", "+00:00")
                if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
                    s = s[:-2] + ":" + s[-2:]
                lat = f" (+{(datetime.now(timezone.utc) - datetime.fromisoformat(s)).total_seconds():.2f}s)"
        except Exception:  # noqa: BLE001
            pass
        log.info(
            "webull-listener[SHADOW] trader=%s %s %s status=%s cat=%s filled=%s@%s boid=%s%s",
            trader_user_id, payload.get("symbol"), payload.get("side"),
            payload.get("order_status"), payload.get("category"),
            payload.get("filled_qty"), payload.get("filled_price"),
            payload.get("order_id"), lat,
        )
        return

    # Live mode — persist + fanout. Isolated so a bad event can't kill the stream.
    try:
        _persist_and_fanout(trader_user_id, broker_account_id, creds, payload)
    except Exception:  # noqa: BLE001
        log.exception(
            "webull-listener[%s] live persist/fanout failed for order %s",
            trader_user_id, payload.get("order_id"),
        )


# ── lifecycle ───────────────────────────────────────────────────────────────
def find_placed_order(db, trader_user_id, *ids):
    """Our row for an order this feed just reported, matched on EITHER id.

    Webull gives an order two identifiers and we hold the other one:
    ``WebullAdapter.place_order`` returns our client_order_id as
    ``broker_order_id`` (it is the handle every later cancel / replace / read
    uses), while this feed keys on Webull's own ``order_id``.

    Looking up by the feed's id ALONE meant the SELECT always missed for orders
    we placed ourselves, so their status was never updated here: the contract
    filled at the broker and our row sat at SUBMITTED, with no order.placed
    event to push the fill to the UI. Only the app-originated guard fired,
    which correctly avoided a duplicate row but left the real one stale.
    """
    # Compare BOTH spellings of the client id. Webull caps client_order_id at 32
    # chars so WebullAdapter sends the Order UUID with its dashes stripped, and
    # stores that form -- but the day-orders endpoint echoes it back in canonical
    # dashed form ("f7f6ebe3-104f-..."), while order-history returns the stripped
    # one. A plain string compare therefore missed on the poll path: the lookup
    # failed, and once the app-originated marker expired (120s) the listener
    # inserted the order a SECOND time, rebuilt from the feed -- typed STOCK with
    # strike/expiry/right NULL, which also breaks realized P&L by a factor of 100.
    wanted: list[str] = []
    for i in ids:
        if not i:
            continue
        for form in (i, i.replace("-", "")):
            if form and form not in wanted:
                wanted.append(form)
    if not wanted:
        return None
    return db.execute(
        select(Order)
        .where(or_(*[Order.broker_order_id == i for i in wanted]))
        .where(Order.user_id == trader_user_id)
        .where(Order.parent_order_id.is_(None))
        .order_by(Order.created_at.desc())
        .limit(1)
    ).scalars().first()


async def start_all_listeners() -> None:
    """Spawn a listener for every active TRADER with a connected Webull account.
    No-op unless webull_direct_enabled — so with the flag off this is inert."""
    if not _enabled():
        return
    with SessionLocal() as db:
        rows = db.execute(
            select(BrokerAccount.user_id, BrokerAccount.id)
            .join(User, User.id == BrokerAccount.user_id)
            .where(
                User.role == UserRole.TRADER,
                User.is_active.is_(True),
                BrokerAccount.broker == BrokerName.WEBULL,
                BrokerAccount.connection_status == "connected",
            )
        ).all()
    for user_id, acct_id in rows:
        start_listener(user_id, acct_id)


def start_listener(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    if not _enabled():
        return
    existing = _tasks.get(trader_user_id)
    if existing and not existing.done():
        stop_listener(trader_user_id)

    loop = _main_loop
    try:
        loop = asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        on_loop = False
    if loop is None:
        log.warning("webull-listener[%s] no loop bound; start is a no-op", trader_user_id)
        return

    _generation[trader_user_id] = _generation.get(trader_user_id, 0) + 1

    # Shadow mode is the DEFAULT, and its symptom — the trader's fills are
    # detected and logged but never mirrored — is indistinguishable from a broken
    # integration unless you already know to look for it. Say so once, loudly, at
    # every listener start rather than only per-event at INFO.
    #
    # Scope note for whoever reads this in the logs: shadow gates the TRADER-side
    # persist+fanout only. A SUBSCRIBER executing mirrors on their own direct
    # Webull account is unaffected by this flag.
    if _shadow():
        log.warning(
            "webull-listener[%s] SHADOW MODE: this trader's orders will be "
            "detected and logged but NOT persisted and NOT mirrored to "
            "subscribers. Set WEBULL_DIRECT_SHADOW_MODE=false to go live.",
            trader_user_id,
        )

    def _spawn() -> None:
        task = loop.create_task(_run_listener(trader_user_id, broker_account_id))
        _tasks[trader_user_id] = task
        # Poll backstop runs alongside the stream (dedup keeps them from
        # double-firing). It's the reliable detection path while Webull's
        # push scope is disabled; harmless when the stream also works.
        if _poll_enabled():
            ptask = loop.create_task(_run_poller(trader_user_id, broker_account_id))
            _poll_tasks[trader_user_id] = ptask
        _set_state(trader_user_id, "connecting")

    if on_loop:
        _spawn()
    else:
        loop.call_soon_threadsafe(_spawn)


def stop_listener(trader_user_id: uuid.UUID) -> None:
    # Bump generation FIRST so any in-flight callback from the old client drops.
    _generation[trader_user_id] = _generation.get(trader_user_id, 0) + 1
    client = _clients.pop(trader_user_id, None)
    if client is not None:
        try:
            client.request_stop()   # closes channel → stream loop returns
        except Exception:  # noqa: BLE001
            pass
    task = _tasks.pop(trader_user_id, None)
    if task and not task.done():
        task.cancel()
    ptask = _poll_tasks.pop(trader_user_id, None)
    if ptask and not ptask.done():
        ptask.cancel()
    _poll_baseline.pop(trader_user_id, None)
    _poll_status.pop(trader_user_id, None)
    _set_state(trader_user_id, "disconnected")


async def stop_all_listeners() -> None:
    for tid in list(_tasks.keys()):
        stop_listener(tid)


def has_running_listener(trader_user_id: uuid.UUID) -> bool:
    t = _tasks.get(trader_user_id)
    return t is not None and not t.done()


def running_trader_ids() -> set[uuid.UUID]:
    return {tid for tid, t in list(_tasks.items()) if not t.done()}


async def _run_listener(trader_user_id: uuid.UUID, broker_account_id: uuid.UUID) -> None:
    """Outer loop: load creds → verify → gRPC subscribe (blocking, in a thread)
    → reconnect with backoff. Same shape as snaptrade_listener._run_listener."""
    backoff = _BACKOFF_INITIAL
    while True:
        try:
            creds = _load_creds(broker_account_id)
            if creds is None or not creds.get("app_key") or not creds.get("account_id"):
                _set_state(trader_user_id, "credentials_invalid",
                           error="webull credentials missing or account disconnected")
                await asyncio.sleep(30)
                continue

            generation = _generation.get(trader_user_id, 0)
            try:
                client = await asyncio.to_thread(_build_stoppable_client, creds)
            except Exception as exc:  # noqa: BLE001
                _set_state(trader_user_id, "reconnecting", error=str(exc)[:300])
                await asyncio.sleep(backoff)
                backoff = min(_BACKOFF_MAX, backoff * 2)
                continue

            client.on_events_message = (
                lambda et, st, payload, raw, _tid=trader_user_id,
                _aid=broker_account_id, _gen=generation, _creds=creds:
                _on_order_event(_tid, _aid, _gen, _creds, payload)
            )
            # Log SDK frames at the level the SDK assigns them: Pings are DEBUG
            # (hidden under the default INFO config, so no flood), while subscribe
            # success (INFO) and auth/stream errors (ERROR/FATAL) still surface.
            client.on_log = lambda level, msg, _tid=trader_user_id: log.log(
                level, "webull-listener[%s] SDK: %s", _tid, msg,
            )
            _clients[trader_user_id] = client
            _set_state(trader_user_id, "connected")
            backoff = _BACKOFF_INITIAL

            # Subscribe to ALL the trader's accounts (they may trade on any).
            account_ids = await asyncio.to_thread(_all_account_ids, creds)
            log.info("webull-listener[%s] subscribing to accounts: %s", trader_user_id, account_ids)
            try:
                # Blocks until the stream ends (stopped, or a non-retryable error).
                await asyncio.to_thread(client.do_subscribe, account_ids)
            finally:
                _clients.pop(trader_user_id, None)

            # Stream returned on its own (not cancelled) → reconnect.
            _set_state(trader_user_id, "reconnecting")

        except asyncio.CancelledError:
            c = _clients.pop(trader_user_id, None)
            if c is not None:
                try:
                    c.request_stop()
                except Exception:  # noqa: BLE001
                    pass
            log.info("webull-listener[%s] cancelled", trader_user_id)
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("webull-listener[%s] error", trader_user_id)
            _set_state(trader_user_id, "reconnecting", error=str(exc)[:300])

        await asyncio.sleep(backoff)
        backoff = min(_BACKOFF_MAX, backoff * 2)


__all__ = [
    "bind_loop", "start_all_listeners", "start_listener", "stop_listener",
    "stop_all_listeners", "has_running_listener", "running_trader_ids", "get_status",
]
