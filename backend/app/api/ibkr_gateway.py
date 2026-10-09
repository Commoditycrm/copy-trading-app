"""Hosted IBKR gateway endpoints.

* ``/api/brokers/{id}/ibkr-gateway/status``     — is the slot's gateway signed in?
* ``/api/brokers/{id}/ibkr-gateway/login-url``  — a one-time URL for the Sign in button
* ``/api/brokers/{id}/ibkr-gateway/verify``     — after sign-in: confirm the account and activate
* ``/api/ibkr-gateway/{slot}/{path}``           — the login page itself, proxied from the gateway

See services/ibkr_hosted_gateway.py for the design.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user
from app.brokers import adapter_for
from app.database import SessionLocal, get_db
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.user import User
from app.services import audit, cache
from app.services import ibkr_hosted_gateway as hosted
from app.services.crypto import decrypt_json, encrypt_json

log = logging.getLogger(__name__)

router = APIRouter(tags=["ibkr-gateway"])


def _hosted_account(db: Session, account_id: uuid.UUID, user: User) -> BrokerAccount:
    acct = db.get(BrokerAccount, account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "not_found")
    if acct.broker != BrokerName.IBKR or acct.ibkr_gateway_slot is None:
        raise HTTPException(400, "not_a_hosted_ibkr_account")
    return acct


@router.get("/api/brokers/{account_id}/ibkr-gateway/status")
def gateway_status(
    account_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user),
) -> dict[str, Any]:
    acct = _hosted_account(db, account_id, user)
    st = hosted.auth_status(acct.ibkr_gateway_slot)
    return {
        "slot": acct.ibkr_gateway_slot,
        "signed_in": st["authenticated"],
        "reachable": st["reachable"],
        "connection_status": acct.connection_status,
    }


@router.post("/api/brokers/{account_id}/ibkr-gateway/login-url")
def gateway_login_url(
    account_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user),
) -> dict[str, Any]:
    acct = _hosted_account(db, account_id, user)
    slot = acct.ibkr_gateway_slot
    token = hosted.login_token(user.id, slot)
    return {"url": f"{hosted.login_path(slot)}?gwt={token}", "expires_in_s": hosted.LOGIN_TOKEN_MINUTES * 60}


@router.post("/api/brokers/{account_id}/ibkr-gateway/verify")
def gateway_verify(
    account_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """After the owner signed in: confirm the gateway sees their account, then
    make this the active broker. Idempotent — safe to call on an account that
    is already connected (it just re-checks)."""
    # Local imports: these helpers live beside the connect endpoint.
    from app.api.brokers import (  # noqa: PLC0415
        _deactivate_other_brokers, _lock_user_brokers, _refresh_balance_into,
        _start_trader_listener,
    )

    acct = _hosted_account(db, account_id, user)
    slot = acct.ibkr_gateway_slot
    creds = decrypt_json(acct.encrypted_credentials)

    st = hosted.auth_status(slot)
    if not st["reachable"]:
        raise HTTPException(503, "Your hosted gateway isn't running. Try again in a minute.")
    if not st["authenticated"]:
        raise HTTPException(409, "not_signed_in")

    # Account number: adopt it when the form left it blank and the login sees
    # exactly one account; otherwise it must be among the visible accounts.
    visible = hosted.accounts(slot)
    wanted = str(creds.get("account_id") or "").strip().upper()
    if not wanted:
        if len(visible) == 1:
            wanted = visible[0]
        elif not visible:
            raise HTTPException(409, "not_signed_in")
        else:
            raise HTTPException(
                400, f"This IBKR login has several accounts ({', '.join(visible)}). "
                     "Disconnect and reconnect with the account number you want.",
            )
    elif visible and wanted not in visible:
        raise HTTPException(
            400, f"Account {wanted} isn't one this IBKR login can see ({', '.join(visible)}). "
                 "Disconnect and reconnect with the right account number.",
        )
    creds["account_id"] = wanted

    try:
        info = adapter_for(acct, creds).verify_connection()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"broker_error: {exc}") from exc

    _lock_user_brokers(db, user.id)
    was_connected = acct.connection_status == "connected"
    if not was_connected:
        _deactivate_other_brokers(db, user, request, keep_id=acct.id)
    acct.encrypted_credentials = encrypt_json(creds)
    acct.broker_account_number = info.broker_account_id
    acct.supports_fractional = info.supports_fractional
    acct.connection_status = "connected"
    acct.last_error = None
    try:
        _refresh_balance_into(acct, creds)
    except Exception:  # noqa: BLE001
        log.warning("balance refresh after hosted IBKR verify failed for %s", acct.id, exc_info=True)
    if not was_connected:
        audit.record(
            db, actor_user_id=user.id, action="broker.connected",
            entity_type="broker_account", entity_id=acct.id,
            metadata={"broker": "ibkr", "label": acct.label, "is_paper": acct.is_paper,
                      "account": acct.broker_account_number, "hosted_slot": slot},
            ip_address=client_ip(request),
        )
    db.commit()
    db.refresh(acct)
    cache.invalidate_broker_accounts(user.id)
    if not was_connected:
        _start_trader_listener(user, acct)
    return {"ok": True, "connection_status": acct.connection_status, "account": acct.broker_account_number}


# ── The login page, proxied ─────────────────────────────────────────────────


def _owns_slot(user_id: uuid.UUID, slot: int) -> bool:
    """Sync, in a worker thread: does this user hold this hosted slot?"""
    with SessionLocal() as db:
        return db.execute(
            select(BrokerAccount.id).where(
                BrokerAccount.user_id == user_id, BrokerAccount.ibkr_gateway_slot == slot,
            )
        ).first() is not None


_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "content-length", "content-encoding", "host",
}


@router.api_route(
    "/api/ibkr-gateway/{slot}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def gateway_proxy(slot: int, path: str, request: Request) -> Response:
    # No request-scoped DB session here on purpose: a login page fans out into
    # ~20 parallel asset requests, and a session held across each upstream
    # fetch exhausted the connection pool — with the wait happening ON the
    # event loop, which froze the whole backend (local, 2026-10-08). The one
    # ownership lookup runs in a worker thread with its own short session.
    prefix = f"{hosted.PROXY_PREFIX}/{slot}"
    cookie_name = f"{hosted.COOKIE_PREFIX}{slot}"

    # Auth: a fresh token on the URL (from the Sign in button) is exchanged for a
    # cookie scoped to this slot's prefix, then the browser is sent to the same
    # URL without the token so it never sits in history or Referer headers.
    gwt = request.query_params.get("gwt")
    if gwt:
        if hosted.verify_login_token(gwt, slot) is None:
            raise HTTPException(401, "This sign-in link has expired. Open Kopyya and click Sign in to IBKR again.")
        clean = request.url.remove_query_params("gwt")
        resp = RedirectResponse(url=str(clean.path) + (f"?{clean.query}" if clean.query else ""), status_code=302)
        resp.set_cookie(
            cookie_name, gwt, max_age=hosted.LOGIN_TOKEN_MINUTES * 60, path=prefix,
            httponly=True, samesite="lax", secure=request.url.scheme == "https",
        )
        return resp

    user_id = hosted.verify_login_token(request.cookies.get(cookie_name), slot)
    if user_id is None:
        raise HTTPException(401, "Sign in to Kopyya and use the Sign in to IBKR button on your broker card.")
    if not await run_in_threadpool(_owns_slot, user_id, slot):
        raise HTTPException(403, "This gateway isn't yours.")

    upstream = hosted.gateway_url(slot)
    target = f"{upstream}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"

    # Never send conditional headers upstream: a 304 would hand the browser
    # its cached, UNREWRITTEN copy of a script we patch (local 2026-10-08 —
    # the login bundle kept posting to /api/Authenticator after the fix).
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in ("cookie", "if-none-match", "if-modified-since")
    }
    fwd_headers["accept-encoding"] = "identity"
    fwd_cookies = {k: v for k, v in request.cookies.items() if not k.startswith(hosted.COOKIE_PREFIX)}
    body = await request.body()

    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=False, timeout=30.0) as client:
            r = await client.request(request.method, target, headers=fwd_headers, cookies=fwd_cookies, content=body)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Your hosted gateway didn't answer: {exc}") from exc

    rewrite = hosted.is_rewritable(r.headers.get("content-type"))
    out_headers: list[tuple[str, str]] = []
    for k, v in r.headers.multi_items():
        lk = k.lower()
        if lk in _HOP_BY_HOP:
            continue
        if rewrite and lk in ("etag", "last-modified", "expires", "cache-control"):
            continue  # rewritten bodies are never cacheable
        if lk == "location":
            v = hosted.rewrite_location(v, prefix, upstream)
        elif lk == "set-cookie":
            v = hosted.rewrite_set_cookie(v, prefix)
        out_headers.append((k, v))

    content: bytes = r.content
    if rewrite:
        try:
            content = hosted.rewrite_body(r.content.decode("utf-8"), prefix).encode("utf-8")
        except UnicodeDecodeError:
            pass
        out_headers.append(("cache-control", "no-store"))

    resp = Response(content=content, status_code=r.status_code)
    for k, v in out_headers:
        resp.headers.append(k, v)
    return resp
