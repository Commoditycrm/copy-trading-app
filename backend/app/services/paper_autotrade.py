"""QA automatic Alpaca PAPER trading.

QA_PAPER_AUTOTRADE_ENABLED turns it on (production never sets it) and only
the broker accounts listed in QA_PAPER_AUTOTRADE_ACCOUNTS are touched;
everyone else trades exactly as before.

Every order placed or replaced on a listed account first passes ``check()``:
  - an Alpaca account marked paper, with paper credentials and a PK… key
  - the client pointed at Alpaca's paper host
  - Alpaca's own get_account() number starting PA and matching the number
    recorded when it was connected (re-read at most every IDENTITY_TTL_S)
  - not shared with another user
Any failure refuses the order — nothing is sent.

On a listed account:
  - an approved Discord alert is sent to the broker even when its channel is
    in Discord "paper" (simulate-only) mode; manual channels still wait for
    approval, and every channel setting still applies
  - copies of its orders go only to simulated (fake) accounts and to Alpaca
    paper accounts that pass the same identity check; a live account is
    skipped, never copied into

Orders always stay on their owner's own account: nothing is redirected.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.models.broker_account import BrokerAccount, BrokerName

log = logging.getLogger(__name__)

PAPER_HOST = "https://paper-api.alpaca.markets"
IDENTITY_TTL_S = 60


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


def governs(account_id: uuid.UUID | None) -> bool:
    """Orders on this account are automated as paper and must pass check()."""
    return account_id is not None and env_enabled() and account_id in allowed_ids()


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


# ── the guard on the adapter ──────────────────────────────────────────────


def check(account_id: uuid.UUID, creds: dict, client: Any) -> None:
    """Raise PaperGuardError unless this order may go to the broker."""
    problem = verify_identity(account_id, creds, client)
    if problem:
        log.error("paper_autotrade: refused an order on %s — %s", account_id, problem)
        raise PaperGuardError(f"QA paper guard refused the order: {problem}")


def attach(adapter: Any, account_id: uuid.UUID, creds: dict) -> Any:
    """Make the adapter run check() before every place and replace. Alpaca
    only (other brokers can't be paper here and are refused upstream)."""
    from app.brokers.alpaca import AlpacaAdapter  # noqa: PLC0415

    if isinstance(adapter, AlpacaAdapter):
        def guard(req: Any = None, kind: str = "place") -> None:  # noqa: ARG001
            check(account_id, creds, adapter._c())  # noqa: SLF001
        adapter.paper_guard = guard
    return adapter


# ── copies ────────────────────────────────────────────────────────────────


def copy_target_problem(broker: Any, creds: dict | None) -> str | None:
    """Why a subscriber account may not receive a copy of a governed order."""
    probs = static_problems(broker, None, creds)
    return "; ".join(probs) if probs else None

