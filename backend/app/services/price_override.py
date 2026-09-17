"""Hand-pinned contract prices, for testing the trim ladder without the market.

The ladder's stops and trailing exits only move when the price does, so proving
them against a quiet market means waiting for a move that may not come. Pinning
a price lets the same code run against a number you choose.

── This is a testing tool and it has teeth ─────────────────────────────────────
A pinned price feeds the REAL enforcement path. If live trading is on, a pin
below a stop will place a REAL market order — filled at the REAL price, not the
pinned one. That is the point (it proves placement works), and it is also the
danger: the fill can be nothing like the number that triggered it.

Three things keep that contained:

  * ``discord_price_override_enabled`` is off by default and must stay off in
    production. Every entry point checks it.
  * Pins are per trader and per contract. One trader's pin is invisible to
    everyone else.
  * Pins expire on their own (``_TTL_SECONDS``), so a forgotten one stops
    affecting anything rather than silently distorting a position for days.

Storage is Redis when it's available and process memory otherwise — this is
scratch state for a test session, and losing it on restart is fine.
"""
from __future__ import annotations

import json
import logging
import time
from decimal import Decimal, InvalidOperation

from app.config import get_settings

log = logging.getLogger(__name__)

# A pin is for the few minutes you're watching a test, not for the rest of the
# week. Expiring beats a stale pin quietly distorting a position.
_TTL_SECONDS = 60 * 60

_MEM: dict[str, tuple[Decimal, float]] = {}


def enabled() -> bool:
    return bool(get_settings().discord_price_override_enabled)


def contract_key(symbol, strike=None, right=None, expiry=None) -> str:
    """One stable key per contract. Mirrors how the guard identifies one."""
    right_val = getattr(right, "value", right)
    return "|".join([
        (symbol or "").upper(),
        # format 'f' keeps this positional — normalize() alone renders 500 as
        # "5E+2", which still round-trips but wouldn't match a key built by any
        # other code path that formats a strike the way a human writes it.
        "" if strike is None else format(Decimal(str(strike)).normalize(), "f"),
        (right_val or "").lower() if right_val else "",
        expiry.isoformat() if hasattr(expiry, "isoformat") else (expiry or ""),
    ])


def _redis():
    try:
        from app.services.redis_client import get_redis  # noqa: PLC0415
        return get_redis()
    except Exception:  # noqa: BLE001
        return None


def _k(user_id, key: str) -> str:
    return f"discord:pricepin:{user_id}:{key}"


def set_pin(user_id, key: str, price) -> Decimal:
    """Pin a contract's price. Raises ValueError on anything unusable."""
    if not enabled():
        raise RuntimeError("price pinning is disabled")
    try:
        value = Decimal(str(price))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("not a number") from exc
    if value <= 0:
        raise ValueError("must be greater than 0")

    r = _redis()
    if r is not None:
        try:
            r.setex(_k(user_id, key), _TTL_SECONDS, str(value))
            return value
        except Exception:  # noqa: BLE001
            log.warning("price pin: redis write failed, falling back to memory")
    _MEM[_k(user_id, key)] = (value, time.time() + _TTL_SECONDS)
    return value


def get_pin(user_id, key: str) -> Decimal | None:
    if not enabled():
        return None
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_k(user_id, key))
            if raw is not None:
                return Decimal(raw.decode() if isinstance(raw, bytes) else str(raw))
        except Exception:  # noqa: BLE001
            pass
    hit = _MEM.get(_k(user_id, key))
    if hit is None:
        return None
    value, expires = hit
    if time.time() > expires:
        _MEM.pop(_k(user_id, key), None)
        return None
    return value


def clear_pin(user_id, key: str) -> None:
    r = _redis()
    if r is not None:
        try:
            r.delete(_k(user_id, key))
        except Exception:  # noqa: BLE001
            pass
    _MEM.pop(_k(user_id, key), None)


def clear_all(user_id) -> int:
    """Drop every pin this trader has. The panic button."""
    cleared = 0
    r = _redis()
    if r is not None:
        try:
            for k in r.scan_iter(f"discord:pricepin:{user_id}:*"):
                r.delete(k)
                cleared += 1
        except Exception:  # noqa: BLE001
            pass
    prefix = f"discord:pricepin:{user_id}:"
    for k in [k for k in _MEM if k.startswith(prefix)]:
        _MEM.pop(k, None)
        cleared += 1
    return cleared


def apply_to(user_id, position):
    """The pinned price for this position, or None to use the broker's own.

    Never mutates the position — the caller decides what to do with it, so a
    pin can't leak into anything that wasn't asked to honour it.
    """
    if not enabled():
        return None
    return get_pin(user_id, contract_key(
        position.symbol, position.option_strike,
        getattr(position, "option_right", None), position.option_expiry,
    ))


__all__ = ["apply_to", "clear_all", "clear_pin", "contract_key", "enabled",
           "get_pin", "set_pin"]
