"""Broker connection endpoints (direct broker integration).

Supported brokers
-----------------
- **Alpaca** (direct): paste API key + secret. Realtime via WebSocket.
- **Webull** (direct, OFFICIAL OpenAPI): app_key + app_secret from
  developer.webull.com, plus the account to trade — picked from
  ``POST /api/brokers/webull/accounts`` rather than typed. Realtime via a
  gRPC trade-event stream with a REST poll backstop (see
  app/services/webull_listener.py). Gated by ``webull_direct_enabled``.
- **SnapTrade** (aggregator): hosted-portal OAuth flow. ~20 brokers via
  a single integration. Realtime via 5s polling — SnapTrade itself polls
  the upstream broker, so faster polling on our side buys nothing.

One ACTIVE broker per user
--------------------------
A user can keep several brokers on file but only one is ACTIVE
(``connection_status == "connected"``) at a time. Connecting a new one
makes it active and switches the previous one to ``inactive`` — kept,
keys and all, not deleted — so a trader can go back to it with Activate
instead of re-entering keys. Deactivate pauses the active one. Everything
that trades or listens selects ``connected`` only, so an inactive broker
is invisible to it; one source of truth for the trader's fills remains.

Flow
----
1. ``POST /api/brokers/webull/accounts``  (Webull only)
       Exchange the API keys for the list of accounts they can trade, so
       the user PICKS one. Persists nothing.
2. ``POST /api/brokers/snaptrade/start``  (SnapTrade only)
       Register the SnapTrade user (idempotent — deletes+recreates on
       conflict) and return the hosted connection portal URL.
3. ``POST /api/brokers/snaptrade/finish``  (SnapTrade only)
       Called after the user returns from the portal. We list their
       SnapTrade authorizations, pick the newest one, persist the
       attached account as our BrokerAccount.
4. ``POST /api/brokers``
       Direct-broker path (Alpaca, Webull). SnapTrade goes through the
       start/finish endpoints above.
5. ``GET /api/brokers``
       List my connected accounts.
6. ``POST /api/brokers/{id}/refresh-balance``
       Pull cash/buying_power/equity from the broker into our cached
       snapshot.
7. ``DELETE /api/brokers/{id}``
       Remove the connection. For SnapTrade, also removes the
       authorization on SnapTrade's side as a best-effort cleanup.
"""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user
from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter
from app.brokers.webull import WebullAdapter  # lazy SDK import inside its methods
from app.brokers.ibkr import IBKRAdapter
from app.brokers import snaptrade as snap
from app.brokers.snaptrade import SnapTradeAdapter
from app.config import get_settings
from app.database import get_db
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.user import User, UserRole
from app.schemas.broker import (
    BrokerAccountOut,
    BrokerAccountSettingsIn,
    ConnectBrokerIn,
    FinishSnaptradeIn,
    ListWebullAccountsIn,
    StartSnaptradeIn,
    StartSnaptradeOut,
    WebullAccountOut,
)
from app.services import audit, balance_sync, cache, listeners, snaptrade_listener
from app.services.crypto import decrypt_json, encrypt_json
from app.services.notifications import create_notification
from app.services.redis_client import get_sync_redis

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brokers", tags=["brokers"])


def _credentials_for(payload: ConnectBrokerIn, user_id: uuid.UUID) -> dict[str, Any]:
    """Build the credentials dict that gets Fernet-encrypted onto the
    BrokerAccount."""
    match payload.broker:
        case BrokerName.ALPACA:
            if not payload.alpaca:
                raise HTTPException(422, "alpaca credentials required")
            return payload.alpaca.model_dump()
        case BrokerName.WEBULL:
            # Direct Webull (official OpenAPI). Only offered when the flag is
            # on; the generic connect flow then verifies via WebullAdapter
            # (adapter_for → verify_connection) before persisting. Keys are
            # stored Fernet-encrypted, exactly like Alpaca.
            if not get_settings().webull_direct_enabled:
                raise HTTPException(
                    400, "Direct Webull is not enabled on this server "
                         "(webull_direct_enabled is off).",
                )
            if not payload.webull:
                raise HTTPException(422, "webull credentials required")
            creds = payload.webull.model_dump()
            creds["app_key"] = str(creds.get("app_key", "")).strip()
            creds["app_secret"] = str(creds.get("app_secret", "")).strip()
            creds["account_id"] = str(creds.get("account_id", "")).strip()
            creds["region_id"] = (str(creds.get("region_id", "") or "us").strip()) or "us"
            # Webull's paper (test) environment is a different API host; the
            # adapter and listener route to it when this is set.
            creds["paper"] = bool(payload.webull.paper)
            return creds
        case BrokerName.IBKR:
            if not payload.ibkr:
                raise HTTPException(422, "ibkr credentials required")
            creds = payload.ibkr.model_dump()
            # Validate via a live OAuth call before we persist. A bad
            # consumer/token combo would otherwise sit silently and break
            # every subsequent listener poll + order placement.
            try:
                IBKRAdapter(creds).verify_connection()
            except RuntimeError as exc:
                raise HTTPException(400, str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                log.exception("ibkr verify_connection unexpected failure")
                raise HTTPException(400, f"ibkr_error: {exc}") from exc
            return creds
    raise HTTPException(422, "unknown broker")


def _refresh_balance_into(acct: BrokerAccount, creds: dict[str, Any]) -> None:
    """Best-effort balance refresh onto ``acct``. Delegates to the single source
    of truth in ``services.balance_sync`` — the background sweep and the inline
    stale-refresh on list_my_brokers all go through the same logic (a 429 keeps
    the cached balance; other errors land in last_error)."""
    balance_sync.refresh_account_balance(acct, creds)


# ── SnapTrade connect-session helpers ───────────────────────────────────────
#
# The two-step SnapTrade flow needs to remember the user_secret between
# the "start portal" call and the "finish" call after the user returns.
# We use Redis with a 30-minute TTL — long enough for the user to
# complete the portal flow, short enough that an abandoned session
# auto-cleans.

_SNAPTRADE_SESSION_KEY = "snaptrade:connect:{user_id}"
_SNAPTRADE_SESSION_TTL = 30 * 60  # seconds


def _save_snaptrade_session(user_id: uuid.UUID, payload: dict[str, Any]) -> None:
    get_sync_redis().setex(
        _SNAPTRADE_SESSION_KEY.format(user_id=user_id),
        _SNAPTRADE_SESSION_TTL,
        json.dumps(payload),
    )


def _load_snaptrade_session(user_id: uuid.UUID) -> dict[str, Any] | None:
    raw = get_sync_redis().get(_SNAPTRADE_SESSION_KEY.format(user_id=user_id))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _clear_snaptrade_session(user_id: uuid.UUID) -> None:
    get_sync_redis().delete(_SNAPTRADE_SESSION_KEY.format(user_id=user_id))


def _ensure_snaptrade_configured() -> None:
    if not snap.snaptrade_configured():
        raise HTTPException(
            503,
            "SnapTrade is not configured on this server "
            "(SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY).",
        )




def _register_or_reset_snaptrade_user(user_id: uuid.UUID) -> str:
    """Register the SnapTrade user, dealing with the 'already exists'
    case by deleting + re-registering. Returns the userSecret.

    SnapTrade returns the userSecret exactly once, at registration —
    there's no get-by-id endpoint. So if we've lost the secret (no
    BrokerAccount, no Redis session), the only path is delete + re-
    register. That's fine because our user.id namespace is ours.

    Errors are classified so the caller can return a useful message:
      - 401 ('Unable to verify signature') → bad SNAPTRADE_* env vars.
        We raise a 502 with the SnapTrade error so the user knows to
        check their server config rather than retry endlessly.
      - 4xx with a dupe-user signal → delete + retry.
      - anything else → re-raise to be handled by the route as 502.
    """
    from snaptrade_client.exceptions import ApiException

    uid_str = str(user_id)
    try:
        return snap.register_user(uid_str)
    except ApiException as exc:
        status_code = getattr(exc, "status", None)
        body = getattr(exc, "body", None) or {}
        detail = body.get("detail") if isinstance(body, dict) else None
        code = body.get("code") if isinstance(body, dict) else None

        # 401 with code 1076 = signature verification failed (HMAC built
        # from the consumer_key didn't match). This is *always* a config
        # problem (wrong creds, trailing whitespace, swapped fields).
        # Don't bother trying delete + retry — it'll just 401 again.
        if status_code == 401:
            raise HTTPException(
                502,
                f"snaptrade_auth_failed: {detail or 'Unauthorized'} "
                f"(SnapTrade code={code}). Check SNAPTRADE_CLIENT_ID and "
                f"SNAPTRADE_CONSUMER_KEY in your backend .env — code 1076 "
                f"specifically means the consumer key is wrong.",
            ) from exc

        # Heuristic for 'user already exists' — SnapTrade has used a few
        # different error codes/messages over the years. We accept any
        # 4xx with a hint pointing at the user_id collision, otherwise
        # we bail rather than blindly deleting state we shouldn't.
        msg = str(detail or "").lower()
        looks_like_dupe = (
            (400 <= (status_code or 0) < 500)
            and ("already" in msg or "exists" in msg or "duplicate" in msg)
        )
        if not looks_like_dupe:
            raise HTTPException(
                502,
                f"snaptrade_error: {detail or exc} (status={status_code}, code={code})",
            ) from exc

        log.info(
            "snaptrade register_user(%s) reports duplicate; deleting + retrying",
            user_id,
        )
        try:
            snap._build_client().authentication.delete_snap_trade_user(  # noqa: SLF001
                user_id=uid_str
            )
        except ApiException:
            log.warning(
                "snaptrade delete_snap_trade_user(%s) also failed — re-registering anyway",
                user_id,
            )
        try:
            return snap.register_user(uid_str)
        except ApiException as exc2:
            raise HTTPException(
                502,
                f"snaptrade_error_after_reset: {getattr(exc2, 'body', exc2)}",
            ) from exc2


def _user_broker_lock_key(user_id: uuid.UUID) -> int:
    """Stable signed 64-bit key for ``pg_advisory_xact_lock``, derived from the
    user id. Every endpoint that replaces this user's broker connection hashes
    to the SAME key, so they serialise against each other — a /finish racing a
    direct connect is just as dangerous as two of either.

    blake2b, NOT Python's ``hash()``: string hashing is salted per process, so a
    ``hash()``-derived key differs between uvicorn workers and the lock would
    only serialise requests that happened to land on the same one. The web tier
    runs ``--workers N``, so that is exactly the case this must cover.
    """
    digest = hashlib.blake2b(f"broker_connect:{user_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _lock_user_brokers(db: Session, user_id: uuid.UUID) -> None:
    """Serialise this user's broker-connection mutations for the rest of THIS
    transaction (released automatically on commit/rollback).

    Why it matters: connecting is replace-on-connect, so two concurrent calls
    both read "no existing row" (or both evict), and the user ends up with TWO
    BrokerAccount rows. The copy engine iterates a subscriber's accounts with no
    dedup, so two rows means EVERY trader trade is mirrored TWICE onto the same
    brokerage account. A double-click on Connect, a retried request, or React
    Strict Mode double-firing an effect is enough to cause it.

    No-ops outside PostgreSQL — advisory locks are a PG feature, and the SQLite
    used in tests is single-connection anyway.
    """
    try:
        if db.get_bind().dialect.name != "postgresql":
            return
    except Exception:  # noqa: BLE001 — no bind resolvable; nothing to lock against
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"), {"k": _user_broker_lock_key(user_id)}
    )


INACTIVE = "inactive"


def _deactivate_other_brokers(
    db: Session, user: User, request: Request, keep_id: uuid.UUID | None = None,
    reason: str = "replaced",
) -> list[BrokerAccount]:
    """One ACTIVE broker per user: switch every other connected account to
    ``inactive`` and stop the trader's listener. Returns the accounts changed.
    SnapTrade accounts are the exception — they are removed, not paused.

    Accounts are no longer deleted when another is connected — the trader keeps
    their keys and can switch back with Activate instead of re-entering them.
    ``inactive`` is invisible to everything that trades: listeners, the copy
    engine, positions, stops and balance sync all select
    ``connection_status == "connected"``. Order history keeps its links.
    """
    rows = list(db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars())
    changed = [a for a in rows if a.id != keep_id]
    for acct in changed:
        if acct.broker == BrokerName.SNAPTRADE:
            # SnapTrade connections are never kept inactive: their link to the
            # underlying broker stays live on SnapTrade's side (and may be
            # billed) while paused here. Replaced exactly as before multi-broker
            # support — the row is removed locally; order history survives
            # (broker_account_id is SET NULL on delete).
            audit.record(
                db, actor_user_id=user.id, action="broker.replaced",
                entity_type="broker_account", entity_id=acct.id,
                metadata={"broker": acct.broker.value, "label": acct.label},
                ip_address=client_ip(request),
            )
            db.delete(acct)
            continue
        acct.connection_status = INACTIVE
        audit.record(
            db, actor_user_id=user.id, action="broker.deactivated",
            entity_type="broker_account", entity_id=acct.id,
            metadata={"broker": acct.broker.value, "label": acct.label, "reason": reason},
            ip_address=client_ip(request),
        )
    if changed:
        db.flush()
        # Stop whichever listener was servicing the trader. Safe to call
        # unconditionally — listeners.stop_listener tries every backend and
        # no-ops when nothing is running.
        if user.role == UserRole.TRADER:
            try:
                listeners.stop_listener(user.id)
            except Exception:  # noqa: BLE001
                log.exception("stop_listener while deactivating a broker failed")
    return changed


def _release_webull_app_key(
    db: Session, acct: BrokerAccount, creds: dict[str, Any], actor: User, request: Request,
) -> list[BrokerAccount]:
    """Deactivate every OTHER connected Webull account on this app key.

    Webull allows one live events subscription per app key, so two connected
    accounts sharing a key — two users here, or the same keys entered twice —
    leave one listener refused forever. The account being connected/activated
    wins; the others go inactive (keys kept, one click to take back). Any user's
    account, not just the actor's: the conflict is at Webull, per key.

    Stored keys are encrypted, so each connected Webull row is decrypted and
    compared — a handful of rows, on connect/activate only.
    """
    if acct.broker != BrokerName.WEBULL:
        return []
    key = str(creds.get("app_key") or "").strip()
    if not key:
        return []
    released = []
    for other in db.execute(
        select(BrokerAccount).where(
            BrokerAccount.broker == BrokerName.WEBULL,
            BrokerAccount.connection_status == "connected",
            BrokerAccount.id != acct.id,
        )
    ).scalars():
        try:
            other_key = str(decrypt_json(other.encrypted_credentials).get("app_key") or "").strip()
        except Exception:  # noqa: BLE001 — unreadable keys can't be the same key
            continue
        if other_key != key:
            continue
        other.connection_status = INACTIVE
        audit.record(
            db, actor_user_id=actor.id, action="broker.deactivated",
            entity_type="broker_account", entity_id=other.id,
            metadata={"broker": other.broker.value, "label": other.label,
                      "reason": "app_key_in_use", "owner_user_id": str(other.user_id),
                      "taken_by_account": str(acct.id)},
            ip_address=client_ip(request),
        )
        if other.user_id != actor.id:
            try:
                create_notification(
                    db, user_id=other.user_id, type="broker.deactivated",
                    message=(
                        f"Webull ({other.broker_account_number or other.label}) was "
                        "deactivated: its app key was activated on another account. "
                        "Webull allows only one live connection per key."
                    ),
                    metadata={"broker_account_id": str(other.id), "reason": "app_key_in_use"},
                )
            except Exception:  # noqa: BLE001
                log.warning("could not notify %s of app-key release", other.user_id, exc_info=True)
        released.append(other)
    if released:
        db.flush()
    return released


def _after_release(released: list[BrokerAccount]) -> str | None:
    """Post-commit side of _release_webull_app_key: stop the released owners'
    listeners (so the stream is freed for the new one) and build the toast."""
    if not released:
        return None
    for other in released:
        cache.invalidate_broker_accounts(other.user_id)
        try:
            listeners.stop_listener(other.user_id)   # no-op for non-traders
        except Exception:  # noqa: BLE001
            log.exception("stop_listener for released app key failed (%s)", other.user_id)
    names = ", ".join(o.broker_account_number or o.label for o in released)
    plural = "connections" if len(released) > 1 else "connection"
    return (f"Deactivated the other Webull {plural} using this app key ({names}) — "
            "Webull allows only one live connection per key.")


def _start_trader_listener(user: User, acct: BrokerAccount) -> None:
    """Start the trader's listener inline when this process runs background
    workers. In the web/worker split the worker's listeners.reconcile() picks
    the active account up within one interval instead — the web container must
    never run a listener of its own (duplicate poller, double-processed fills).
    """
    if user.role == UserRole.TRADER and get_settings().run_background_workers:
        try:
            listeners.start_listener(user.id, acct.id)
        except Exception:  # noqa: BLE001
            log.exception("failed to start listener for broker %s", acct.id)


@router.post("/snaptrade/webhook")
async def snaptrade_webhook(request: Request, background: BackgroundTasks) -> dict:
    """Inbound SnapTrade webhook — UNAUTHENTICATED (SnapTrade calls this,
    not a logged-in user). Powered by SnapTrade's Trade Detection feature:
    SnapTrade polls the connected broker at the subscribed cadence and
    POSTs here the instant it detects a new executed order.

    Verification: SnapTrade's dashboard listener form takes ONLY a URL —
    there's no shared-secret field. They sign webhooks instead (see their
    "verify webhook signatures" docs), so the secret may arrive in a
    HEADER, not the body. We therefore:
      • accept calls that carry no body secret (the normal SnapTrade case),
      • only reject if a body secret IS present and mismatches (covers a
        future SnapTrade change or a manual test),
      • log the header names on each call so we can wire up proper
        signature verification once we observe the real scheme.

    Safe-by-design even without full signature verification: a forged call
    can at most trigger an extra poll, which fetches REAL orders from
    SnapTrade and dedups by broker_order_id — it cannot inject fake trades.

    We don't branch on the exact event type — any event carrying a
    ``userId`` we recognise triggers an immediate poll of that trader's
    orders. The poll runs as a background task so we return 200 fast
    (SnapTrade retries on slow/failed responses, and fanout can take a
    second or two)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    # DIAGNOSTIC: log the full headers + body of every webhook so we can
    # see exactly how SnapTrade signs/structures its payload, then add
    # strict signature verification matching their actual scheme. The
    # SnapTrade dashboard's listener form has NO secret field, so they
    # verify via signature (header), not a shared secret in the body —
    # comparing a body field against our .env secret was wrong and was
    # the source of the 401 on their test deliveries.
    # Log only the SHAPE of the request (header + body key names), never the
    # values — the body and signature headers can carry tokens / PII and this
    # endpoint is internet-facing + unauthenticated. Full values go to DEBUG
    # only, for operators who explicitly raise the log level while wiring up
    # SnapTrade's (still-undocumented-to-us) signature scheme.
    sig_header_names = [
        k for k in request.headers.keys()
        if any(t in k.lower() for t in ("sign", "signature", "snaptrade", "webhook", "hmac", "digest"))
    ]
    log.info(
        "snaptrade webhook | header_names=%s | sig_header_names=%s | body_keys=%s",
        list(request.headers.keys()),
        sig_header_names,
        list(body.keys()) if isinstance(body, dict) else type(body).__name__,
    )

    # NOTE: signature verification intentionally not enforced yet — we
    # accept the call and trigger a re-poll, which only ever fetches REAL
    # orders from SnapTrade and dedups (a forged call can't inject fake
    # trades). Once the log above shows SnapTrade's actual signature
    # header/scheme, we'll verify it here and reject mismatches.

    event_type = body.get("eventType") or body.get("type") or "unknown"
    user_id_raw = body.get("userId") or body.get("user_id")

    # SnapTrade sends a no-user test ping when you first configure the
    # listener — ack with 200 so the dashboard marks it healthy.
    if not user_id_raw:
        log.info("snaptrade webhook: test/no-user event=%s", event_type)
        return {"ok": True}

    try:
        trader_user_id = uuid.UUID(str(user_id_raw))
    except (ValueError, TypeError):
        log.warning("snaptrade webhook: unparseable userId=%r", user_id_raw)
        return {"ok": True}  # ack; nothing actionable

    log.info(
        "snaptrade webhook: event=%s userId=%s — scheduling immediate poll",
        event_type, trader_user_id,
    )
    # Fire-and-forget so we return 200 immediately. The periodic backstop
    # poll catches anything this misses.
    background.add_task(snaptrade_listener.poll_now_for_trader, trader_user_id)
    return {"ok": True}


@router.post("/snaptrade/start", response_model=StartSnaptradeOut)
def snaptrade_start(
    payload: StartSnaptradeIn,
    user: User = Depends(current_user),
) -> StartSnaptradeOut:
    """Step 1 of the SnapTrade connect flow. Registers (or re-registers)
    the SnapTrade user, caches the userSecret + label in a 30-min
    connect session, and returns the hosted portal URL for the
    frontend to redirect into."""
    _ensure_snaptrade_configured()

    user_secret = _register_or_reset_snaptrade_user(user.id)

    s = get_settings()
    custom_redirect = f"{s.frontend_base_url}/brokers?snaptrade_connected=1"
    try:
        portal_url = snap.make_login_url(
            user_id=str(user.id),
            user_secret=user_secret,
            custom_redirect=custom_redirect,
            broker_slug=payload.broker_slug,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("snaptrade make_login_url failed")
        raise HTTPException(502, detail="snaptrade_error") from exc

    _save_snaptrade_session(user.id, {
        "user_secret": user_secret,
        "label":       payload.label,
        "paper":       bool(payload.paper),
        "broker_slug": payload.broker_slug,
    })

    return StartSnaptradeOut(portal_url=portal_url)


@router.post("/snaptrade/finish", response_model=BrokerAccountOut,
             status_code=status.HTTP_201_CREATED)
def snaptrade_finish(
    payload: FinishSnaptradeIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BrokerAccount:
    """Step 2: called by the frontend after the user returns from the
    portal. We resolve which authorization (and account) was just added
    by picking the newest one, persist it as a BrokerAccount, and start
    the listener.

    Picking 'newest' is robust to the user adding multiple brokers in
    sequence — the most recent authorization is always the one they
    just finished. Edge case: if the portal closed without completing,
    list_brokerage_authorizations returns whatever was there before;
    we surface that as a clean 'no connection found' error.

    Concurrency: we acquire a per-user advisory lock at the top of the
    transaction. Without it, two concurrent /finish calls (most likely
    cause: React Strict Mode double-firing the redirect-back effect)
    both run _deactivate_other_brokers before either commits, and the
    user ends up with two BrokerAccount rows pointing at the same
    SnapTrade authorization — each with its own polling listener
    double-processing every trade. The advisory lock serialises per-
    user so the second call sees the first's row and short-circuits.
    Released automatically on commit/rollback.

    It shares ONE key with the direct-connect endpoint (_lock_user_brokers), so
    a /finish racing a direct connect serialises too — previously each derived
    its own key and the two could interleave freely. That key is also blake2b
    now: this used to hash the user id with Python's ``hash()``, which is salted
    per process, so the key differed between uvicorn workers and the lock only
    ever serialised requests that landed on the same one."""
    _ensure_snaptrade_configured()
    _lock_user_brokers(db, user.id)

    # If a SnapTrade BrokerAccount already exists for this user, the
    # other concurrent /finish call already ran. Return that row
    # instead of creating a duplicate. We check by user_id + broker
    # rather than by authorization_id because the encrypted_credentials
    # blob is opaque to a WHERE clause — but one-broker-per-user means
    # the user_id+broker pair is unique enough.
    #
    # Only a CONNECTED row created moments ago counts. With several brokers on
    # file, an older SnapTrade connection can legitimately sit INACTIVE; matching
    # it here would hand that stale row back and the new connection would
    # silently never be made. The race this guards against is seconds wide.
    from datetime import timedelta  # noqa: PLC0415
    existing_snap = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.broker == BrokerName.SNAPTRADE,
            BrokerAccount.connection_status == "connected",
            BrokerAccount.created_at >= datetime.now(timezone.utc) - timedelta(minutes=2),
        ).order_by(BrokerAccount.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    if existing_snap is not None:
        log.info(
            "snaptrade /finish: existing SnapTrade account %s found for user %s "
            "(likely concurrent /finish race); returning existing row",
            existing_snap.id, user.id,
        )
        return existing_snap

    session = _load_snaptrade_session(user.id)
    if session is None:
        raise HTTPException(
            400,
            "no_snaptrade_session — start the portal flow first via "
            "POST /api/brokers/snaptrade/start",
        )

    user_secret = session["user_secret"]
    # Prefer the label captured at /start (authoritative — see schema:
    # "label carries through from start"). The /finish payload can carry a
    # stale "SnapTrade" default when the portal redirect lands in a new tab
    # and loses the sessionStorage-stashed label, so it must NOT win.
    label = session.get("label") or payload.label or "SnapTrade"
    paper = bool(session.get("paper", False))

    try:
        auths = snap.list_authorizations(str(user.id), user_secret)
    except Exception as exc:  # noqa: BLE001
        log.exception("snaptrade list_authorizations failed")
        raise HTTPException(502, detail="snaptrade_error") from exc

    if not auths:
        raise HTTPException(
            400,
            "no_connection_found — the portal closed without completing. "
            "Click 'Connect via SnapTrade' to try again.",
        )

    # Pick newest by created_date (SnapTrade sorts ascending; reverse).
    auths_sorted = sorted(
        auths,
        key=lambda a: str(_attr_safe(a, "created_date", "createdDate", default="")),
        reverse=True,
    )
    newest = auths_sorted[0]
    auth_id = str(_attr_safe(newest, "id", "authorizationId"))
    brokerage = _attr_safe(newest, "brokerage", default={}) or {}
    brokerage_name = str(_attr_safe(brokerage, "name", default="SnapTrade Brokerage"))
    brokerage_slug = str(_attr_safe(brokerage, "slug", default=""))
    # SnapTrade may downgrade our requested ``connection_type="trade"``
    # to ``"read"`` when the chosen broker doesn't support placement
    # via SnapTrade (Webull is the well-known example). We record this
    # on the account so the trade panel can show an inline warning,
    # and we surface a 400 only for subscribers — for traders, read is
    # enough to feed the listener; for subscribers, read makes every
    # mirror order fail Forbidden which is a worse failure than
    # blocking the connect now.
    auth_type = str(_attr_safe(newest, "type", default="read")).lower()

    try:
        accounts = snap.list_accounts(str(user.id), user_secret)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, detail="snaptrade_error") from exc

    matching = [
        a for a in accounts
        if str(_attr_safe(_attr_safe(a, "brokerage_authorization", default={}), "id",
                          default=_attr_safe(a, "brokerage_authorization_id", default=""))
              ) == auth_id
    ] or accounts  # fall back to all accounts if the link can't be resolved
    if not matching:
        raise HTTPException(
            400,
            "no_account_found — SnapTrade authorization exists but has no "
            "accounts attached. This usually means the broker session "
            "ended before account sync completed.",
        )
    account_obj = matching[0]
    account_id = str(_attr_safe(account_obj, "id", "accountId"))
    account_number = str(_attr_safe(account_obj, "number", "account_number",
                                    default="") or "")

    creds: dict[str, Any] = {
        "snaptrade_user_id":     str(user.id),
        "snaptrade_user_secret": user_secret,
        "authorization_id":      auth_id,
        "account_id":            account_id,
        "brokerage_name":        brokerage_name,
        "brokerage_slug":        brokerage_slug,
        "paper":                 paper,
        "auth_type":             auth_type,
    }

    # Subscribers can't function with a read-only SnapTrade connection —
    # every mirror order would 403. Block the connect with an explicit
    # error so they know which broker to pick instead, rather than
    # silently succeeding and failing every subsequent fanout.
    if user.role == UserRole.SUBSCRIBER and auth_type != "trade":
        _clear_snaptrade_session(user.id)
        raise HTTPException(
            400,
            f"snaptrade_read_only — {brokerage_name} only supports read-only "
            f"access through SnapTrade, so mirror orders can't be placed on "
            f"this account. Pick a different broker (Robinhood, Tradier, "
            f"Alpaca, …) or connect Alpaca directly with API keys.",
        )

    # One ACTIVE broker per user: the one being connected replaces the current
    # one as active; the old one is kept, inactive, for switching back.
    _deactivate_other_brokers(db, user, request)

    acct = BrokerAccount(
        user_id=user.id,
        broker=BrokerName.SNAPTRADE,
        label=label,
        is_paper=paper,
        supports_fractional=True,
        encrypted_credentials=encrypt_json(creds),
        connection_status="pending",
        broker_account_number=account_number or None,
        # Denormalized so the trader-facing fanout table can show the
        # underlying broker (Webull / Robinhood / IBKR / …) without
        # paying a Fernet decrypt per child row on every page render.
        brokerage_name=brokerage_name or None,
    )
    try:
        info = adapter_for(acct, creds).verify_connection()
        if info.broker_account_id:
            acct.broker_account_number = info.broker_account_id
        acct.connection_status = "connected"
        _refresh_balance_into(acct, creds)
    except Exception as exc:  # noqa: BLE001
        audit.record(
            db, actor_user_id=user.id, action="broker.connect_failed",
            metadata={"broker": "snaptrade", "error": str(exc)[:480]},
            ip_address=client_ip(request),
        )
        db.commit()
        raise HTTPException(400, f"snaptrade_verify_failed: {exc}")

    db.add(acct)
    db.flush()
    audit.record(
        db, actor_user_id=user.id, action="broker.connected",
        entity_type="broker_account", entity_id=acct.id,
        metadata={
            "broker": "snaptrade",
            "label": label,
            "brokerage": brokerage_name,
            "account": acct.broker_account_number,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(acct)
    cache.invalidate_broker_accounts(user.id)
    _clear_snaptrade_session(user.id)

    if user.role == UserRole.TRADER:
        try:
            listeners.start_listener(user.id, acct.id)
        except Exception:  # noqa: BLE001
            log.exception("failed to start snaptrade listener")

    return acct


def _attr_safe(obj: Any, *names: str, default: Any = None) -> Any:
    """Tolerant attribute/key lookup — SDK responses are sometimes dict,
    sometimes typed. Local copy so api/brokers.py doesn't import from
    a private helper in app/brokers/snaptrade.py."""
    for n in names:
        if isinstance(obj, dict):
            v = obj.get(n)
        else:
            v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _webull_error_message(exc: BaseException) -> str:
    """Turn a Webull SDK failure into something the user can act on.

    The one that matters is the token handshake. Webull issues an access token in
    PENDING status and it stays unusable until the ACCOUNT OWNER authorises it
    from their Webull app — until then every call fails with a raw
    ``ERROR_INIT_TOKEN ... status:PENDING``, which tells the user nothing about
    what they are supposed to do. This is not an edge case; it is what happens on
    every first connect.
    """
    raw = str(exc)
    low = raw.lower()
    if "error_init_token" in low or "status not verified" in low or "pending" in low:
        return (
            "Webull needs you to authorise this API key before it can be used. "
            "Open the Webull app, approve the pending API/token authorisation "
            "request, then try again here. (The key stays in PENDING status "
            "until you do.)"
        )
    if "error_check_token" in low and ("invalid" in low or "expired" in low):
        return (
            "Webull rejected this API token as invalid or expired. Generate a "
            "fresh key at developer.webull.com and reconnect."
        )
    if "too_many_requests" in low or "too many requests" in low or "429" in raw:
        # Every connect / "Load my accounts" click re-runs Webull's token
        # handshake, and the same key used from two places (local and QA) shares
        # one limit — rapid retries are what trip it.
        return (
            "Webull is rate-limiting this API key (too many requests). Wait a "
            "minute or two, then try once — each attempt re-runs Webull's token "
            "check, and the same key used elsewhere (another environment or "
            "browser) counts against the same limit."
        )
    if "unauthorized" in low or "invalid credentials" in low or "401" in raw:
        return (
            "Webull rejected these credentials. Check the app key and secret are "
            "copied exactly, that the key is enabled for the Trading API, and "
            "that Paper / Live matches the key: paper (test) keys only work in "
            "Paper mode and live keys only in Live."
        )
    return f"broker_error: {raw}"


@router.post("/webull/accounts", response_model=list[WebullAccountOut])
def list_webull_accounts(
    payload: ListWebullAccountsIn,
    user: User = Depends(current_user),
) -> list[dict[str, Any]]:
    """Step 1 of the direct-Webull connect: list the accounts these API keys can
    trade, so the user PICKS the one to link.

    Why this endpoint exists. A Webull app_key reaches EVERY account under that
    login — Cash, Margin, IRA, Futures — and which one we trade is decided purely
    by the ``account_id`` in the stored credentials. That id is not the account
    number shown anywhere in the Webull app, so the previous free-text field
    asked users to guess: a real-but-wrong id passes ``verify_connection``
    cleanly, and from then on every mirror order trades in the wrong account with
    nothing to flag it. Balances come back with the list because equity is what
    actually distinguishes a funded account from an empty one.

    Nothing is persisted here. The keys are used for this call and discarded;
    they are only stored (encrypted) if the user goes on to connect.

    Side benefit: this is also where Webull's first-time token/2FA challenge now
    surfaces — before any connect attempt, and well before anything could touch
    the user's existing broker.
    """
    if not get_settings().webull_direct_enabled:
        raise HTTPException(
            400, "Direct Webull is not enabled on this server "
                 "(webull_direct_enabled is off).",
        )
    creds = {
        "app_key": payload.app_key.strip(),
        "app_secret": payload.app_secret.strip(),
        "region_id": (payload.region_id or "us").strip() or "us",
        "paper": bool(payload.paper),
    }
    try:
        accounts = WebullAdapter(creds).list_accounts(with_balances=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("webull list_accounts failed for user %s", user.id, exc_info=True)
        raise HTTPException(400, _webull_error_message(exc)) from exc
    if not accounts:
        raise HTTPException(
            400,
            "These Webull API keys authenticated but returned no tradable "
            "accounts. Check that the key is enabled for the Trading API.",
        )
    return accounts


@router.post("", response_model=BrokerAccountOut, status_code=status.HTTP_201_CREATED)
def connect(
    payload: ConnectBrokerIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BrokerAccount:
    # Direct Webull is available to BOTH roles: a TRADER uses it as a real-time
    # fill-signal source (read/stream via webull_listener), and a SUBSCRIBER now
    # executes their mirror orders on it directly (WebullAdapter implements the
    # write path; mirror fills sync via services.webull_subscriber_reconciler).
    # The webull_direct_enabled server flag still gates it for everyone inside
    # _credentials_for below, so this stays fully inert with the flag off.
    creds = _credentials_for(payload, user.id)

    # Build an unsaved row so we can run verify_connection() against it.
    # Don't persist if the broker rejects — keeps ghost rows out of the UI.
    acct = BrokerAccount(
        user_id=user.id,
        broker=payload.broker,
        label=payload.label,
        is_paper=bool(creds.get("paper", True)),
        supports_fractional=True,
        encrypted_credentials=encrypt_json(creds),
        connection_status="pending",
    )

    # VERIFY BEFORE EVICTING. A failed connect must leave the user's EXISTING
    # broker exactly as it was. It previously didn't: the old eviction step ran
    # first and the failure handler's db.commit() (written to persist the audit
    # row) also committed those pending DELETEs — so a rejected attempt silently
    # disconnected the working broker and copy trading stopped with a
    # "skipped_no_broker". Direct Webull made that routine rather than rare: its
    # first connect normally fails while the user approves the 2FA push in the
    # Webull app, and the retry is the one that succeeds.
    if payload.broker == BrokerName.WEBULL:
        # The user is acting now: give them a real sign-in, not a back-off wait.
        from app.brokers.webull import clear_sign_in_backoff  # noqa: PLC0415
        clear_sign_in_backoff(str(creds.get("app_key") or ""))
    try:
        info = adapter_for(acct, creds).verify_connection()
        acct.broker_account_number = info.broker_account_id
        acct.supports_fractional = info.supports_fractional
        acct.connection_status = "connected"
        # Pull balance immediately so the UI doesn't have a blank row.
        _refresh_balance_into(acct, creds)
    except Exception as exc:  # noqa: BLE001
        # Drop anything this request touched before writing the audit row, so
        # the commit below can only ever persist the audit itself.
        db.rollback()
        audit.record(
            db, actor_user_id=user.id, action="broker.connect_failed",
            metadata={"broker": payload.broker.value, "error": str(exc)[:480]},
            ip_address=client_ip(request),
        )
        db.commit()
        raise HTTPException(
            400,
            _webull_error_message(exc) if payload.broker == BrokerName.WEBULL
            else f"broker_error: {exc}",
        )

    # Verified — only NOW is it safe to replace what they already had.
    #
    # Everything from here to the commit is the critical section: two concurrent
    # connects that both get past this point leave the user with TWO
    # BrokerAccount rows, and the copy engine mirrors every trade once PER ROW —
    # so the subscriber's account gets doubled on every trade. Take the per-user
    # lock so the second request waits, then evicts the first's row and replaces
    # it (last writer wins, exactly one account either way).
    #
    # Deliberately locked HERE rather than at the top of the handler: locking
    # before verify_connection would hold a DB connection across the broker
    # round-trip, which for Webull includes the token/2FA flow and can run for
    # seconds. Verification has no side effects, so letting both requests verify
    # and serialising only the write is both safe and cheap.
    _lock_user_brokers(db, user.id)

    # One ACTIVE broker per user. The current one goes inactive (kept, not
    # deleted); doing it before the broker.connected audit keeps the trail
    # reading naturally: deactivated → connected.
    _deactivate_other_brokers(db, user, request)

    # Reconnecting an account that is already on file (same broker, same
    # account at that broker) refreshes that row instead of adding a twin — the
    # copy engine mirrors once per CONNECTED row, and history stays on one id.
    same = None
    if acct.broker_account_number:
        same = db.execute(
            select(BrokerAccount).where(
                BrokerAccount.user_id == user.id,
                BrokerAccount.broker == acct.broker,
                BrokerAccount.broker_account_number == acct.broker_account_number,
            )
        ).scalars().first()
    if same is not None:
        for field in ("label", "is_paper", "supports_fractional", "encrypted_credentials",
                      "connection_status"):
            setattr(same, field, getattr(acct, field))
        for field in ("cash", "buying_power", "total_equity", "balance_updated_at"):
            if hasattr(acct, field) and getattr(acct, field) is not None:
                setattr(same, field, getattr(acct, field))
        same.last_error = None
        acct = same
    else:
        db.add(acct)
    db.flush()
    released = _release_webull_app_key(db, acct, creds, user, request)
    audit.record(
        db, actor_user_id=user.id, action="broker.connected",
        entity_type="broker_account", entity_id=acct.id,
        metadata={"broker": payload.broker.value, "label": payload.label,
                  "is_paper": acct.is_paper, "account": acct.broker_account_number},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(acct)
    cache.invalidate_broker_accounts(user.id)
    acct.notice = _after_release(released)

    # If the connecting user is a trader, spin up the listener so trades
    # placed directly at the broker propagate to subscribers. The
    # dispatcher routes to Alpaca-WebSocket or Webull-poll as needed.
    #
    # Only start it inline when THIS process runs background workers
    # (single-process dev, or the dedicated worker). In the web/worker split
    # the WEB container must NOT start a listener — it would run a duplicate
    # poller in the wrong process (double broker calls + double-processed
    # fills), and it can't start a task in the worker anyway. There the
    # worker's periodic listeners.reconcile() picks the new broker up within
    # one interval.
    _start_trader_listener(user, acct)
    return acct


@router.get("/webull-usage")
def webull_usage_summary(
    minutes: int = Query(5, ge=1, le=60),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """How many requests went to Webull for YOUR app key(s) in the last
    ``minutes``, split by what made them (Positions page, order poll, P&L poller,
    auto-trim, sign-in …) and by Webull endpoint. Counted across the web and
    worker processes; see services/webull_usage.py. Reads only our own counters
    — it never calls Webull itself."""
    from app.services import webull_usage  # noqa: PLC0415

    keys = []
    for acct in db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id, BrokerAccount.broker == BrokerName.WEBULL,
        )
    ).scalars():
        try:
            keys.append(str(decrypt_json(acct.encrypted_credentials).get("app_key") or ""))
        except Exception:  # noqa: BLE001
            continue
    out = webull_usage.summary(keys, minutes)
    out["has_webull"] = bool(keys)
    return out


@router.get("/features")
def broker_features(user: User = Depends(current_user)) -> dict:
    """Client-facing broker feature flags for the Brokers page. Lets the picker
    hide the direct-Webull option when the server has it disabled (connect would
    otherwise 400)."""
    return {"webull_direct_enabled": bool(get_settings().webull_direct_enabled)}


# A balance older than this is refreshed inline when the account list is read,
# so the Dashboard (which reads this list) shows a current figure without a
# broker call on every load. The Brokers page still force-refreshes via
# /refresh-balance, and the background sweep (balance_sync) covers idle accounts.
_BALANCE_STALE_S = 120


@router.get("", response_model=list[BrokerAccountOut])
def list_my_brokers(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[BrokerAccount]:
    accts = list(db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == user.id)
        .order_by(BrokerAccount.created_at.desc())
    ).scalars())
    # Part A: refresh any connected account whose balance is stale so the
    # Dashboard shows current equity. Throttled by balance_updated_at (frequent
    # loads don't hammer the broker; a 429 keeps the cached value). Best-effort —
    # a refresh failure never fails the list.
    now = datetime.now(timezone.utc)
    dirty = False
    for acct in accts:
        if acct.connection_status != "connected":
            continue
        age = (now - acct.balance_updated_at).total_seconds() if acct.balance_updated_at else None
        if age is None or age > _BALANCE_STALE_S:
            try:
                balance_sync.refresh_account_balance(
                    acct, decrypt_json(acct.encrypted_credentials)
                )
                dirty = True
            except Exception:  # noqa: BLE001
                pass  # refresh is best-effort; never block the list
    if dirty:
        db.commit()
    # Attach each account's effective Day P&L refresh interval (transient — not a
    # column) so the frontend reads one broker-chosen cadence instead of
    # branching on broker names. Alpaca reuses the runtime knob.
    from app.brokers.capabilities import effective_day_pnl_interval_s  # noqa: PLC0415
    for acct in accts:
        acct.day_pnl_refresh_interval_s = effective_day_pnl_interval_s(acct.broker)
    return accts


@router.post("/{account_id}/refresh-balance", response_model=BrokerAccountOut)
def refresh_balance(
    account_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    # Set by the Brokers page's 30s auto-poll. Auto-polls skip the audit row —
    # otherwise every open tab writes a broker.balance_refreshed entry twice a
    # minute and buries the deliberate, user-initiated refreshes.
    auto: bool = Query(False),
) -> BrokerAccount:
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "not_found")
    creds = decrypt_json(acct.encrypted_credentials)
    _refresh_balance_into(acct, creds)
    if not auto:
        audit.record(
            db, actor_user_id=user.id, action="broker.balance_refreshed",
            entity_type="broker_account", entity_id=acct.id,
            ip_address=client_ip(request),
        )
    db.commit()
    db.refresh(acct)
    return acct


@router.patch("/{account_id}/settings", response_model=BrokerAccountOut)
def update_broker_account_settings(
    account_id: uuid.UUID,
    payload: BrokerAccountSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BrokerAccount:
    """Update the listener-gating flags on a broker account.

    Partial: any field left unset on the payload is unchanged. Owner-only
    (caller must own the account). Used by the Brokers page checkboxes
    (Auto Pull Orders + Bring open/Filled orders) so each user can decide
    what their broker's listener actually persists + fans out.
    """
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "not_found")

    changes: dict[str, bool] = {}
    if payload.auto_pull_orders is not None and payload.auto_pull_orders != acct.auto_pull_orders:
        acct.auto_pull_orders = payload.auto_pull_orders
        changes["auto_pull_orders"] = payload.auto_pull_orders
    if payload.bring_open_orders is not None and payload.bring_open_orders != acct.bring_open_orders:
        acct.bring_open_orders = payload.bring_open_orders
        changes["bring_open_orders"] = payload.bring_open_orders
    if payload.bring_filled_orders is not None and payload.bring_filled_orders != acct.bring_filled_orders:
        acct.bring_filled_orders = payload.bring_filled_orders
        changes["bring_filled_orders"] = payload.bring_filled_orders

    if changes:
        audit.record(
            db, actor_user_id=user.id, action="broker.settings_updated",
            entity_type="broker_account", entity_id=acct.id,
            metadata=changes, ip_address=client_ip(request),
        )

    db.commit()
    db.refresh(acct)
    return acct


@router.post("/{account_id}/deactivate", response_model=BrokerAccountOut)
def deactivate_broker(
    account_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BrokerAccount:
    """Pause a broker connection without deleting it.

    Keys stay stored (encrypted); nothing trades, copies or listens through it
    until it is activated again. Positions already open at that broker stay
    open — the app just stops managing them.
    """
    _lock_user_brokers(db, user.id)
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "not_found")
    if acct.broker == BrokerName.SNAPTRADE:
        # Pausing locally would leave SnapTrade's link to the broker live (and
        # possibly billed) with nothing using it. Disconnect instead.
        raise HTTPException(
            400, "SnapTrade connections can't be deactivated — disconnect it instead.",
        )
    if acct.connection_status == INACTIVE:
        return acct
    acct.connection_status = INACTIVE
    audit.record(
        db, actor_user_id=user.id, action="broker.deactivated",
        entity_type="broker_account", entity_id=acct.id,
        metadata={"broker": acct.broker.value, "label": acct.label, "reason": "user"},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(acct)
    cache.invalidate_broker_accounts(user.id)
    if user.role == UserRole.TRADER:
        try:
            listeners.stop_listener(user.id)
        except Exception:  # noqa: BLE001
            log.exception("stop_listener after broker deactivate failed")
    return acct


@router.post("/{account_id}/activate", response_model=BrokerAccountOut)
def activate_broker(
    account_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BrokerAccount:
    """Make a stored broker the active one; the current active one goes inactive.

    The stored keys are verified first — a key can expire, or a Webull token
    lapse, while the account sat inactive — and nothing is switched unless the
    broker accepts them, so a failed activate leaves the current broker active.
    """
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "not_found")
    if acct.connection_status == "connected":
        return acct
    if acct.broker == BrokerName.WEBULL and not get_settings().webull_direct_enabled:
        raise HTTPException(400, "Direct Webull is not enabled on this server "
                                 "(webull_direct_enabled is off).")

    creds = decrypt_json(acct.encrypted_credentials)
    if acct.broker == BrokerName.WEBULL:
        from app.brokers.webull import clear_sign_in_backoff  # noqa: PLC0415
        clear_sign_in_backoff(str(creds.get("app_key") or ""))
    # Verify outside the lock: for Webull this can include the token/2FA flow.
    try:
        info = adapter_for(acct, creds).verify_connection()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            400,
            _webull_error_message(exc) if acct.broker == BrokerName.WEBULL
            else f"broker_error: {exc}",
        ) from exc

    _lock_user_brokers(db, user.id)
    _deactivate_other_brokers(db, user, request, keep_id=acct.id, reason="switched")
    released = _release_webull_app_key(db, acct, creds, user, request)
    acct.connection_status = "connected"
    acct.last_error = None
    if info.broker_account_id:
        acct.broker_account_number = info.broker_account_id
    try:
        _refresh_balance_into(acct, creds)
    except Exception:  # noqa: BLE001
        log.warning("balance refresh on activate failed for %s", acct.id, exc_info=True)
    audit.record(
        db, actor_user_id=user.id, action="broker.activated",
        entity_type="broker_account", entity_id=acct.id,
        metadata={"broker": acct.broker.value, "label": acct.label},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(acct)
    cache.invalidate_broker_accounts(user.id)
    acct.notice = _after_release(released)
    _start_trader_listener(user, acct)
    return acct


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_broker(
    account_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "not_found")

    # For SnapTrade, also remove the authorization on SnapTrade's side
    # so we're not leaving an orphan upstream that keeps polling the
    # user's broker. Best-effort — a failure here doesn't block our
    # local delete because the local DB row is the source of truth for
    # whether we still consider this user connected.
    if acct.broker == BrokerName.SNAPTRADE:
        try:
            creds = decrypt_json(acct.encrypted_credentials)
            snap.delete_authorization(
                creds["snaptrade_user_id"],
                creds["snaptrade_user_secret"],
                creds["authorization_id"],
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "snaptrade delete_authorization on broker delete failed "
                "(continuing with local delete)",
                exc_info=True,
            )

    audit.record(
        db, actor_user_id=user.id, action="broker.deleted",
        entity_type="broker_account", entity_id=acct.id,
        metadata={"broker": acct.broker.value, "label": acct.label},
        ip_address=client_ip(request),
    )
    # Only the ACTIVE account has a listener. Deleting an inactive one must not
    # stop the listener that is servicing the active broker.
    was_active_trader = user.role == UserRole.TRADER and acct.connection_status == "connected"
    db.delete(acct)
    db.commit()
    cache.invalidate_broker_accounts(user.id)

    # Stop whichever listener was running for the trader (Alpaca,
    # Webull, or SnapTrade). Dispatcher tries all — safe even if none
    # was active.
    if was_active_trader:
        try:
            listeners.stop_listener(user.id)
        except Exception:  # noqa: BLE001
            log.exception("stop_listener after broker delete failed")
