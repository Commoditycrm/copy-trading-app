"""Direct-Webull real-time trade listener (gRPC).

Streams a master trader's order events from Webull's OpenAPI over gRPC (~0.2s
from fill to us, vs SnapTrade's minutes) and — once out of shadow mode — hands
each new order to ``copy_engine.fanout_threadsafe`` exactly like
``snaptrade_listener`` does. Subscribers still EXECUTE via SnapTrade; this only
replaces the trader-side DETECTION signal.

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

from sqlalchemy import select, text

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
from app.services import listener_state
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
# order_ids that already existed when the poller started — the trader's
# earlier-in-day activity, which we must NOT replay as fresh signals.
_poll_baseline: dict[uuid.UUID, set[str]] = {}
# order_id → last status we acted on (post-baseline), so we only process a
# genuine new order or a status transition (submit → fill → cancel), not the
# same unchanged row every cycle.
_poll_status: dict[uuid.UUID, dict[str, str]] = {}

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


# Webull's "Query Day Orders" endpoint (our list_today_orders) is limited to
# 10 requests / 30s PER APP ID — shared across every account under one app_key.
# That's 1 call / 3s. We poll once per account per cycle, so the safe cycle
# length scales with account count. Keep a hard floor above 3s and a headroom
# factor so a burst/retry never crosses the line.
_DAYORDERS_MIN_INTERVAL_S = 3.5          # per-call floor (>3s ceiling + margin)
_DAYORDERS_PER_ACCOUNT_S = 3.3           # 30s/10 with ~10% headroom, per account


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
    t = str(s or "").upper()
    if t in ("LIMIT", "LMT"):
        return OrderType.LIMIT
    if t in ("STOP", "STP"):
        return OrderType.STOP
    if t in ("STOP_LIMIT", "STP_LMT", "STOP_LOSS_LIMIT"):
        return OrderType.STOP_LIMIT
    return OrderType.MARKET


# ── cached REST client (per app_key) ─────────────────────────────────────────
# CRITICAL: TradeClient.__init__ runs the SDK's token flow (init_token →
# fetch_token_from_server, a network call to Webull's auth endpoint). Building a
# fresh client on EVERY poll cycle (every 5s) hammered that endpoint → 429s,
# repeated 2FA challenges, and eventually a VERIFY_FAILURE_EXCEED_LIMIT lockout
# that stopped order detection entirely. So we build ONE client per app_key and
# reuse it across cycles; the token flow then runs once per TTL, not every poll.
_TRADE_CLIENT_TTL_S = 1800.0  # rebuild every 30 min to refresh auth
_trade_clients: dict[str, tuple[Any, datetime]] = {}
_trade_client_lock = threading.Lock()


def _webull_trade_client(creds: dict[str, Any]):
    from webull.core.client import ApiClient  # noqa: PLC0415
    from webull.trade.trade_client import TradeClient  # noqa: PLC0415
    app_key = creds["app_key"]
    now = datetime.now(timezone.utc)
    with _trade_client_lock:
        cached = _trade_clients.get(app_key)
        if cached is not None and (now - cached[1]).total_seconds() < _TRADE_CLIENT_TTL_S:
            return cached[0]
        from app.brokers.webull import set_per_account_token_dir  # noqa: PLC0415
        api_client = ApiClient(app_key, creds["app_secret"], creds.get("region_id", "us"))
        # Stop the SDK writing ./webull_trade_sdk.log — the container root FS is
        # read-only (Errno 30). _init_logger skips its file handler when a logger
        # is already marked set. See app/brokers/webull.py:_suppress_sdk_file_logger.
        api_client._stream_logger_set = True  # noqa: SLF001
        # Per-app_key token file so multiple traders' Webull accounts don't
        # collide on the shared token.txt (→ 417 INVALID_TOKEN).
        set_per_account_token_dir(api_client, app_key)
        client = TradeClient(api_client)   # token flow runs HERE — once per TTL
        _trade_clients[app_key] = (client, now)
        return client


def _invalidate_trade_client(creds: dict[str, Any]) -> None:
    """Drop the cached client so the next call rebuilds it (re-auths). Call only
    on AUTH failures — NOT on 429s (a 429 means throttled, not bad auth;
    rebuilding would re-hit the token endpoint and make throttling worse)."""
    with _trade_client_lock:
        _trade_clients.pop(creds.get("app_key"), None)


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

        existing = db.execute(
            select(Order)
            .where(Order.broker_order_id == broker_order_id)
            .where(Order.user_id == trader_user_id)
            .where(Order.parent_order_id.is_(None))
            .order_by(Order.created_at.desc())
            .limit(1)
        ).scalars().first()

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
def _list_today_orders(creds: dict[str, Any], account_id: str, page_size: int = 30) -> list[dict]:
    """One page of the account's orders for today, newest-first. Returns [] on
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

            orders: list[dict] = []
            for aid in account_ids:
                orders.extend(await asyncio.to_thread(_list_today_orders, creds, aid))

            # First cycle: record what already exists as the baseline (history)
            # and fan nothing out.
            if trader_user_id not in _poll_baseline:
                _poll_baseline[trader_user_id] = {
                    str(o.get("order_id")) for o in orders if o.get("order_id")
                }
                log.info("webull-poll[%s] primed baseline with %d existing order(s)",
                         trader_user_id, len(_poll_baseline[trader_user_id]))
                await asyncio.sleep(interval)
                continue

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
        except asyncio.CancelledError:
            log.info("webull-poll[%s] cancelled", trader_user_id)
            raise
        except Exception:  # noqa: BLE001
            log.exception("webull-poll[%s] cycle failed", trader_user_id)

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
