"""Broker connection endpoints (direct broker integration).

Supported brokers
-----------------
- **Alpaca** (direct): paste API key + secret. Realtime via WebSocket.
- **Webull** (direct, unofficial): username + password + MFA + 6-digit
  trade PIN. Realtime via 2s polling (see app/services/webull_listener.py).
- **SnapTrade** (aggregator): hosted-portal OAuth flow. ~20 brokers via
  a single integration. Realtime via 5s polling — SnapTrade itself polls
  the upstream broker, so faster polling on our side buys nothing.

One-broker-per-user
-------------------
A user can only have one connected broker at a time. Connecting a new
one *replaces* any existing connection — we delete the old row, stop
its listener, and start the new one. This keeps copy-trading semantics
unambiguous (one source of truth for the trader's fills) and matches
the UI shape, which only shows the connect form when no broker is
attached.

Flow
----
1. ``POST /api/brokers/webull/start-mfa``  (Webull only)
       Trigger Webull to send the user an MFA code. Stateless.
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
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user
from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter
from app.brokers import snaptrade as snap
from app.brokers.snaptrade import SnapTradeAdapter
from app.brokers.webull import WebullAdapter, login_with_mfa, request_mfa
from app.config import get_settings
from app.database import get_db
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.user import User, UserRole
from app.schemas.broker import (
    BrokerAccountOut,
    ConnectBrokerIn,
    FinishSnaptradeIn,
    StartSnaptradeIn,
    StartSnaptradeOut,
    StartWebullMfaIn,
    StartWebullMfaOut,
)
from app.services import audit, cache, listeners
from app.services.crypto import decrypt_json, encrypt_json
from app.services.redis_client import get_sync_redis

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brokers", tags=["brokers"])


def _webull_device_id(user_id: uuid.UUID) -> str:
    """Stable device fingerprint per app user. Webull binds MFA codes
    to the requesting device's ``_did`` — get_mfa and the follow-up
    login MUST use the same value or Webull rejects the login with an
    empty body (manifests as ``Expecting value: line 1 column 1`` from
    the SDK's response.json() call). Deriving from user.id via uuid5
    makes the value deterministic across both endpoints without
    storing anything in Redis."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"webull-did-{user_id}"))


def _credentials_for(payload: ConnectBrokerIn, user_id: uuid.UUID) -> dict[str, Any]:
    """Build the credentials dict that gets Fernet-encrypted onto the
    BrokerAccount. For Webull this runs the full login flow because we
    can't store a username/password alone — we need the session tokens
    returned by Webull's login endpoint."""
    match payload.broker:
        case BrokerName.ALPACA:
            if not payload.alpaca:
                raise HTTPException(422, "alpaca credentials required")
            return payload.alpaca.model_dump()
        case BrokerName.WEBULL:
            if not payload.webull:
                raise HTTPException(422, "webull credentials required")
            w = payload.webull
            device_id = _webull_device_id(user_id)
            try:
                return login_with_mfa(
                    username=w.username,
                    password=w.password,
                    mfa_code=w.mfa_code,
                    trade_pin=w.trade_pin,
                    paper=w.paper,
                    device_id=device_id,
                )
            except ValueError as exc:
                # login_with_mfa raises ValueError with a user-safe message
                # for bad password / wrong MFA / bad PIN.
                raise HTTPException(400, str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                log.exception("webull login_with_mfa unexpected failure")
                raise HTTPException(400, f"webull_error: {exc}") from exc
    raise HTTPException(422, "unknown broker")


def _refresh_balance_into(acct: BrokerAccount, creds: dict[str, Any]) -> None:
    """Best-effort. Errors are recorded into last_error, not raised."""
    try:
        adapter = adapter_for(acct, creds)
        if isinstance(adapter, (AlpacaAdapter, WebullAdapter, SnapTradeAdapter)):
            bal = adapter.get_balance_snapshot()
            acct.cash = bal["cash"]
            acct.buying_power = bal["buying_power"]
            acct.total_equity = bal["total_equity"]
            acct.currency = bal["currency"]
            acct.balance_updated_at = datetime.now(timezone.utc)
    except Exception as exc:  # noqa: BLE001
        acct.last_error = f"balance fetch failed: {str(exc)[:400]}"


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


def _evict_existing_brokers(
    db: Session, user: User, request: Request
) -> None:
    """One-broker-per-user: delete any existing broker_account rows for
    this user and stop their listeners. Called before inserting a new
    one. Audits each eviction so the trail shows why the old connection
    went away.

    Existing Order rows survive — broker_account_id is SET NULL on
    delete (see Order model). The trader's history doesn't disappear
    just because they switched brokers."""
    existing = list(db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == user.id)
    ).scalars())
    for acct in existing:
        audit.record(
            db, actor_user_id=user.id, action="broker.replaced",
            entity_type="broker_account", entity_id=acct.id,
            metadata={"broker": acct.broker.value, "label": acct.label},
            ip_address=client_ip(request),
        )
        db.delete(acct)
    if existing:
        db.flush()
        # Stop whichever listener was servicing the trader. Safe to call
        # unconditionally — listeners.stop_listener tries both Alpaca
        # and Webull backends, no-ops when nothing is running.
        if user.role == UserRole.TRADER:
            try:
                listeners.stop_listener(user.id)
            except Exception:  # noqa: BLE001
                log.exception("stop_listener during broker replacement failed")


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
        raise HTTPException(502, f"snaptrade_error: {exc}") from exc

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
    both run _evict_existing_brokers before either commits, and the
    user ends up with two BrokerAccount rows pointing at the same
    SnapTrade authorization — each with its own polling listener
    double-processing every trade. The advisory lock serialises per-
    user so the second call sees the first's row and short-circuits.
    Released automatically on commit/rollback."""
    from sqlalchemy import text

    _ensure_snaptrade_configured()
    # pg_advisory_xact_lock takes a bigint; hash to 63-bit positive int.
    lock_key = hash(("snaptrade-finish", str(user.id))) & 0x7FFFFFFFFFFFFFFF
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})

    # If a SnapTrade BrokerAccount already exists for this user, the
    # other concurrent /finish call already ran. Return that row
    # instead of creating a duplicate. We check by user_id + broker
    # rather than by authorization_id because the encrypted_credentials
    # blob is opaque to a WHERE clause — but one-broker-per-user means
    # the user_id+broker pair is unique enough.
    existing_snap = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.broker == BrokerName.SNAPTRADE,
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
    label = payload.label or session.get("label") or "SnapTrade"
    paper = bool(session.get("paper", False))

    try:
        auths = snap.list_authorizations(str(user.id), user_secret)
    except Exception as exc:  # noqa: BLE001
        log.exception("snaptrade list_authorizations failed")
        raise HTTPException(502, f"snaptrade_error: {exc}") from exc

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
        raise HTTPException(502, f"snaptrade_error: {exc}") from exc

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
            f"Alpaca, IBKR, …) or connect Alpaca directly with API keys.",
        )

    # Evict any existing broker first (one-broker-per-user).
    _evict_existing_brokers(db, user, request)

    acct = BrokerAccount(
        user_id=user.id,
        broker=BrokerName.SNAPTRADE,
        label=label,
        is_paper=paper,
        supports_fractional=True,
        encrypted_credentials=encrypt_json(creds),
        connection_status="pending",
        broker_account_number=account_number or None,
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


@router.post("/webull/start-mfa", response_model=StartWebullMfaOut)
def webull_start_mfa(
    payload: StartWebullMfaIn,
    user: User = Depends(current_user),
) -> StartWebullMfaOut:
    """Trigger Webull to send the MFA code. Uses the same per-user
    device_id that the follow-up ``POST /api/brokers`` call will use,
    so Webull recognises the login as coming from the same device that
    requested the code (without this, login fails with an empty-body
    JSON error from the SDK)."""
    device_id = _webull_device_id(user.id)
    try:
        request_mfa(payload.username, paper=payload.paper, device_id=device_id)
    except ValueError as exc:
        # request_mfa already converts SDK JSONDecodeError + obvious
        # rate-limit cases into user-safe ValueError messages.
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"webull_mfa_error: {exc}") from exc
    return StartWebullMfaOut(
        sent=True,
        message="MFA code sent. Enter it on the next step to finish connecting.",
    )


@router.post("", response_model=BrokerAccountOut, status_code=status.HTTP_201_CREATED)
def connect(
    payload: ConnectBrokerIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BrokerAccount:
    creds = _credentials_for(payload, user.id)

    # Enforce one-broker-per-user BEFORE building the new row so the
    # audit ordering reads naturally (replaced → connected).
    _evict_existing_brokers(db, user, request)

    # Build an unsaved row so we can run verify_connection() against it.
    # Don't persist if the broker rejects — keeps ghost rows out of the
    # UI. Note: for Webull, login_with_mfa above already hit the network,
    # so verify_connection is mostly a safety check that the just-
    # received session tokens really work.
    acct = BrokerAccount(
        user_id=user.id,
        broker=payload.broker,
        label=payload.label,
        is_paper=bool(creds.get("paper", True)),
        supports_fractional=True,
        encrypted_credentials=encrypt_json(creds),
        connection_status="pending",
    )

    try:
        info = adapter_for(acct, creds).verify_connection()
        acct.broker_account_number = info.broker_account_id
        acct.supports_fractional = info.supports_fractional
        acct.connection_status = "connected"
        # Pull balance immediately so the UI doesn't have a blank row.
        _refresh_balance_into(acct, creds)
    except Exception as exc:  # noqa: BLE001
        audit.record(
            db, actor_user_id=user.id, action="broker.connect_failed",
            metadata={"broker": payload.broker.value, "error": str(exc)[:480]},
            ip_address=client_ip(request),
        )
        db.commit()
        raise HTTPException(400, f"broker_error: {exc}")

    db.add(acct)
    db.flush()
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

    # If the connecting user is a trader, spin up the listener so trades
    # placed directly at the broker propagate to subscribers. The
    # dispatcher routes to Alpaca-WebSocket or Webull-poll as needed.
    if user.role == UserRole.TRADER:
        try:
            listeners.start_listener(user.id, acct.id)
        except Exception:  # noqa: BLE001
            log.exception("failed to start listener for new broker")

    return acct


@router.get("", response_model=list[BrokerAccountOut])
def list_my_brokers(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[BrokerAccount]:
    return list(db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == user.id)
        .order_by(BrokerAccount.created_at.desc())
    ).scalars())


@router.post("/{account_id}/refresh-balance", response_model=BrokerAccountOut)
def refresh_balance(
    account_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BrokerAccount:
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "not_found")
    creds = decrypt_json(acct.encrypted_credentials)
    _refresh_balance_into(acct, creds)
    audit.record(
        db, actor_user_id=user.id, action="broker.balance_refreshed",
        entity_type="broker_account", entity_id=acct.id,
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(acct)
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
    was_trader = user.role == UserRole.TRADER
    db.delete(acct)
    db.commit()
    cache.invalidate_broker_accounts(user.id)

    # Stop whichever listener was running for the trader (Alpaca,
    # Webull, or SnapTrade). Dispatcher tries all — safe even if none
    # was active.
    if was_trader:
        try:
            listeners.stop_listener(user.id)
        except Exception:  # noqa: BLE001
            log.exception("stop_listener after broker delete failed")
