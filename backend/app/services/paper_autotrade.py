"""QA automatic Alpaca PAPER trading.

Two switches, both needed: QA_PAPER_AUTOTRADE_ENABLED in the environment (so
production, which never sets it, can't turn this on from the admin page) and
the admin toggle stored in app_settings. Only the broker accounts listed in
QA_PAPER_AUTOTRADE_ACCOUNTS are touched; everyone else trades exactly as before.

With the environment switch on, every order placed or replaced on a listed
account first passes ``check()``:
  - an Alpaca account marked paper, with paper credentials and a PK… key
  - the client pointed at Alpaca's paper host
  - Alpaca's own get_account() number starting PA and matching the number
    recorded when it was connected (re-read at most every IDENTITY_TTL_S)
  - not shared with another user
  - under the per-minute and per-day order caps (a tripped cap holds the
    account until an admin resets it)
Any failure refuses the order — nothing is sent.

With the admin toggle also on:
  - an approved Discord alert on a listed account is sent to the broker even
    when its channel is in Discord "paper" (simulate-only) mode; manual
    channels still wait for approval, and every channel setting still applies
  - copies of a listed account's orders go only to simulated (fake) accounts
    and to Alpaca paper accounts that pass the same identity check; a live
    account is skipped, never copied into

Orders always stay on their owner's own account: nothing is redirected.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.models.broker_account import BrokerAccount, BrokerName

log = logging.getLogger(__name__)

TOGGLE_KEY = "qa_paper_autotrade"
PAPER_HOST = "https://paper-api.alpaca.markets"
IDENTITY_TTL_S = 60
TRIP_TTL_S = 24 * 3600


class PaperGuardError(Exception):
    """The order was refused before reaching the broker."""


# ── switches ──────────────────────────────────────────────────────────────


def env_enabled() -> bool:
    return bool(get_settings().qa_paper_autotrade_enabled)


def allowed_ids() -> set[uuid.UUID]:
    out = set()
    for part in (get_settings().qa_paper_autotrade_accounts or "").split(","):
        part = part.strip()
        if part:
            try:
                out.add(uuid.UUID(part))
            except ValueError:
                log.warning("paper_autotrade: ignoring malformed account id %r", part)
    return out


def toggle_on() -> bool:
    from app.services import app_settings  # noqa: PLC0415

    return app_settings.flag(TOGGLE_KEY, False)


def enabled() -> bool:
    """Automation on: environment switch AND admin toggle."""
    return env_enabled() and toggle_on()


def guards(account_id: uuid.UUID | None) -> bool:
    """Orders on this account must pass check() (environment switch on and listed)."""
    return account_id is not None and env_enabled() and account_id in allowed_ids()


def governs(account_id: uuid.UUID | None) -> bool:
    """This account's trading is automated as paper (both switches, listed)."""
    return guards(account_id) and toggle_on()


# ── identity ──────────────────────────────────────────────────────────────


def static_problems(broker: Any, is_paper: bool | None, creds: dict | None) -> list[str]:
    """What's wrong with an account before asking Alpaca anything."""
    if broker == BrokerName.FAKE:
        return []                                       # simulated; never reaches a broker
    if broker != BrokerName.ALPACA:
        return ["not an Alpaca account"]
    probs = []
    if is_paper is False:
        probs.append("account is not marked paper")
    if not creds or creds.get("paper") is not True:
        probs.append("credentials are not paper credentials")
    if not str((creds or {}).get("api_key", "")).startswith("PK"):
        probs.append("API key is not a paper (PK…) key")
    return probs


@dataclass
class _Identity:
    at: float
    number: str | None
    problem: str | None


_identity: dict[uuid.UUID, _Identity] = {}
_lock = threading.Lock()


def _account_row(account_id: uuid.UUID) -> BrokerAccount | None:
    from app.database import SessionLocal  # noqa: PLC0415

    with SessionLocal() as db:
        acct = db.get(BrokerAccount, account_id)
        if acct is not None:
            db.expunge(acct)
        return acct


def _shared(account_id: uuid.UUID) -> bool:
    from app.database import SessionLocal  # noqa: PLC0415
    from app.services import shared_accounts  # noqa: PLC0415

    with SessionLocal() as db:
        acct = db.get(BrokerAccount, account_id)
        return acct is not None and shared_accounts.shared_with_another_user(db, acct)


def verify_identity(account_id: uuid.UUID, creds: dict, client: Any, *, force: bool = False,
                    now: float | None = None) -> str | None:
    """None when this is provably the recorded Alpaca paper account, else why
    not. Alpaca is asked at most every IDENTITY_TTL_S per account."""
    now = now if now is not None else time.monotonic()
    with _lock:
        hit = _identity.get(account_id)
    if hit is not None and not force and now - hit.at < IDENTITY_TTL_S:
        return hit.problem
    acct = _account_row(account_id)
    problem = None
    number = None
    if acct is None:
        problem = "broker account not found"
    else:
        probs = static_problems(acct.broker, acct.is_paper, creds)
        if probs:
            problem = "; ".join(probs)
        elif acct.broker == BrokerName.ALPACA:
            host = str(getattr(getattr(client, "_base_url", None), "value", getattr(client, "_base_url", "")))
            if host.rstrip("/") != PAPER_HOST:
                problem = f"client is not on the paper host ({host or 'unknown'})"
            else:
                try:
                    number = str(client.get_account().account_number)
                except Exception as exc:  # noqa: BLE001
                    problem = f"couldn't confirm the account with Alpaca ({type(exc).__name__})"
                else:
                    recorded = (acct.broker_account_number or "").strip()
                    if not number.startswith("PA"):
                        problem = "Alpaca says this is not a paper account"
                    elif recorded and number != recorded:
                        problem = "Alpaca account number doesn't match the one connected"
        if problem is None and _shared(account_id):
            problem = "this brokerage account is shared with another user"
    with _lock:
        _identity[account_id] = _Identity(now, number, problem)
    return problem


def last_identity(account_id: uuid.UUID) -> dict | None:
    with _lock:
        hit = _identity.get(account_id)
    if hit is None:
        return None
    return {"ok": hit.problem is None, "problem": hit.problem,
            "number_masked": _mask(hit.number), "age_s": int(time.monotonic() - hit.at)}


def _mask(number: str | None) -> str | None:
    return None if not number else f"{number[:2]}***{number[-3:]}"


# ── order volume ──────────────────────────────────────────────────────────


def _redis():
    from app.services.redis_client import get_sync_redis  # noqa: PLC0415

    return get_sync_redis()


def _keys(account_id: uuid.UUID, at: datetime) -> tuple[str, str, str]:
    return (f"paper_autotrade:min:{account_id}:{at:%Y%m%d%H%M}",
            f"paper_autotrade:day:{account_id}:{at:%Y%m%d}",
            f"paper_autotrade:tripped:{account_id}")


def count_and_cap(account_id: uuid.UUID, at: datetime | None = None, r=None) -> str | None:  # noqa: ANN001
    """Count this order; None if it's within the caps, else why not. A cap,
    once hit, holds the account (TRIP_TTL_S) until an admin resets it."""
    at = at or datetime.now(timezone.utc)
    s = get_settings()
    r = r or _redis()
    k_min, k_day, k_trip = _keys(account_id, at)
    tripped = r.get(k_trip)
    if tripped:
        return f"order cap tripped: {tripped.decode() if isinstance(tripped, bytes) else tripped}"
    pipe = r.pipeline(transaction=False)
    pipe.incr(k_min)
    pipe.expire(k_min, 120)
    pipe.incr(k_day)
    pipe.expire(k_day, 2 * 86400)
    n_min, _, n_day, _ = pipe.execute()
    why = None
    if n_min > s.qa_paper_autotrade_max_orders_per_minute:
        why = f"{n_min} orders in one minute (cap {s.qa_paper_autotrade_max_orders_per_minute})"
    elif n_day > s.qa_paper_autotrade_max_orders_per_day:
        why = f"{n_day} orders today (cap {s.qa_paper_autotrade_max_orders_per_day})"
    if why:
        r.setex(k_trip, TRIP_TTL_S, why)
        _audit("paper_autotrade.cap_tripped", account_id, {"reason": why})
        log.error("paper_autotrade: account %s held — %s", account_id, why)
    return why


def counts(account_id: uuid.UUID, r=None) -> dict:  # noqa: ANN001
    at = datetime.now(timezone.utc)
    r = r or _redis()
    k_min, k_day, k_trip = _keys(account_id, at)
    vals = r.mget([k_min, k_day, k_trip])
    dec = (lambda v: v.decode() if isinstance(v, bytes) else v)
    return {"this_minute": int(dec(vals[0]) or 0), "today": int(dec(vals[1]) or 0), "tripped": dec(vals[2])}


def reset_cap(account_id: uuid.UUID, r=None) -> None:  # noqa: ANN001
    (r or _redis()).delete(_keys(account_id, datetime.now(timezone.utc))[2])


# ── the guard on the adapter ──────────────────────────────────────────────


def check(account_id: uuid.UUID, creds: dict, client: Any) -> None:
    """Raise PaperGuardError unless this order may go to the broker."""
    problem = verify_identity(account_id, creds, client)
    if problem is None:
        try:
            problem = count_and_cap(account_id)
        except Exception as exc:  # noqa: BLE001 — can't count → can't prove the cap
            problem = f"order cap unavailable ({type(exc).__name__})"
    if problem:
        _audit("paper_autotrade.order_refused", account_id, {"reason": problem})
        raise PaperGuardError(f"QA paper guard refused the order: {problem}")


def attach(adapter: Any, account_id: uuid.UUID, creds: dict) -> Any:
    """Make the adapter run check() before every place and replace. Alpaca
    only (other brokers can't be paper here and are refused upstream)."""
    from app.brokers.alpaca import AlpacaAdapter  # noqa: PLC0415

    if isinstance(adapter, AlpacaAdapter):
        adapter.paper_guard = lambda: check(account_id, creds, adapter._c())  # noqa: SLF001
    return adapter


# ── copies ────────────────────────────────────────────────────────────────


def copy_target_problem(broker: Any, creds: dict | None) -> str | None:
    """Why a subscriber account may not receive a copy of a governed order."""
    probs = static_problems(broker, None, creds)
    return "; ".join(probs) if probs else None


def _audit(action: str, account_id: uuid.UUID, metadata: dict) -> None:
    from app.database import SessionLocal  # noqa: PLC0415
    from app.services import audit  # noqa: PLC0415

    try:
        with SessionLocal() as db:
            audit.record(db, actor_user_id=None, action=action, entity_type="broker_account",
                         entity_id=account_id, metadata=metadata)
            db.commit()
    except Exception:  # noqa: BLE001 — the refusal stands even if the audit row can't be written
        log.exception("paper_autotrade: audit %s failed", action)
