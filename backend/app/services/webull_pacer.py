"""Space Webull reads so they never land in the same instant.

Webull keeps a counter per endpoint, per app key (developer.webull.com/apis/
docs/rate-limits): Account Positions, Account Balance, Order History, Open
Orders and Order Detail each allow 2 calls per 2 seconds. What it refuses is
calls close together, not volume — rejections 32ms and 129ms apart at well
under the rate (see webull.py, _POSITIONS_TTL_S).

Six parts of the app read positions on their own clocks, in two processes:
the P&L poller, auto-trim, the trailing-stop loop, a close from Positions, the
Positions page and Day P&L. The display reads share one result through Redis;
the reads that decide something deliberately read live. So two of those and
the page inside 2 seconds is a 429 — 30 an hour on QA 2026-10-20, 8 of them on
a close or an alert order the trader was waiting on.

So every read of one of those endpoints first reserves a slot: at least
``GAP_S`` after the last one for the same app key and endpoint, across every
process (one Redis key each, reserved atomically). A caller whose slot is in
the future waits for it — under a second, normally — instead of being refused
and retrying. Order placement, replace and cancel are on a far larger counter
(600/60s) and are never paced.

Best effort by contract: no Redis, a wait that would be longer than
``MAX_WAIT_S``, or a call on an event loop's own thread (where sleeping would
stall everything else) all go out at once, exactly as before.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# 2 per 2 seconds, and never two at once: one per GAP_S. The margin is wide on
# purpose. A slot is when a call LEAVES; it reaches Webull a variable moment
# later, so at 1.05s apart a third call could land just inside 2s of the first
# — QA 2026-10-20, two order-detail reads refused while paced 1.05s apart. At
# 1.25s the third call is 2.5s after the first.
GAP_S = 1.25
# Longer than this and the call goes out unpaced: a caller should not sit for
# seconds behind a queue, and a display read has its snapshot to fall back on.
MAX_WAIT_S = 4.0
_KEY = "webull:pace:{}:{}"

# The endpoints with the 2/2s counter, by the SDK's action path, and the counter
# each draws on. Matched by suffix so /openapi/... and /trading/... spellings
# both count. The order queries take turns as ONE: the order poll lists today's
# orders every few seconds, and order-detail reads paced only against each other
# were still refused beside it (QA 2026-10-20) — while positions and balance,
# paced on their own, were not.
_PACED = (
    ("assets/positions", "positions"),
    ("assets/balance", "balance"),
    ("orders/list-today", "orders"),
    ("orders/list-open", "orders"),
    ("orders/history", "orders"),
    ("order/detail", "orders"),
)

# Reserve the next slot, atomically: max(now, last + gap). Returns the slot (ms),
# or -1 when it is further off than max_wait — then nothing is reserved and the
# caller does not wait. ``now`` is the caller's clock: the web and worker
# processes run on one host, so they share it.
_RESERVE = """
local now = tonumber(ARGV[3])
local last = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = math.max(now, last + tonumber(ARGV[1]))
if slot - now > tonumber(ARGV[2]) then return -1 end
redis.call('SET', KEYS[1], slot, 'PX', tonumber(ARGV[1]) + tonumber(ARGV[2]) + 1000)
return slot
"""


def paced(action: str | None) -> str | None:
    """The counter this endpoint draws on, or None when it is not paced."""
    a = (action or "").rstrip("/")
    for suffix, counter in _PACED:
        if a.endswith(suffix):
            return counter
    return None


def _on_event_loop_thread() -> bool:
    import asyncio  # noqa: PLC0415

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _redis():
    from app.services.redis_client import get_sync_redis  # noqa: PLC0415

    return get_sync_redis()


QUEUE_FULL = -1.0


def wait_turn(app_hash: str, action: str | None, *, sleep=time.sleep, clock=time.time) -> float:
    """Wait for this call's slot. Returns the seconds waited (0 when not paced),
    or QUEUE_FULL when the queue was too long and the call goes out at once."""
    endpoint = paced(action)
    if endpoint is None or not app_hash or _on_event_loop_thread():
        return 0.0
    now = int(clock() * 1000)
    try:
        slot = int(_redis().eval(_RESERVE, 1, _KEY.format(app_hash, endpoint),
                                 int(GAP_S * 1000), int(MAX_WAIT_S * 1000), now))
    except Exception:  # noqa: BLE001 — pacing must never break a broker call
        log.debug("webull pacer: no slot (Redis?) — sending unpaced", exc_info=True)
        return 0.0
    if slot < 0:
        log.info("webull pacer: %s queue is over %.1fs for key=%s — sending unpaced",
                 endpoint, MAX_WAIT_S, app_hash[:8])
        return QUEUE_FULL
    wait = (slot - now) / 1000.0
    if wait > 0:
        sleep(wait)
    return wait


__all__ = ["GAP_S", "MAX_WAIT_S", "QUEUE_FULL", "paced", "wait_turn"]
