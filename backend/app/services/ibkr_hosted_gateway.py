"""Kopyya-hosted IBKR Client Portal Gateways.

IBKR lets individual accounts reach its API only through the Client Portal
Gateway, a Java process the account owner must sign into through a browser
once per trading day. Making every subscriber run that on their own machine
(plus a private network to reach it) proved too much to ask, so we run the
gateways ourselves: a fixed pool of containers on the server
(``ibkr-gw-1`` … ``ibkr-gw-N`` in docker-compose, profile ``ibkr``), each
holding at most ONE IBKR login.

Connecting an IBKR account in "hosted" mode assigns the account a free slot
and stores ``gateway_url=https://ibkr-gw-<slot>:5000`` in its credentials —
from there the adapter, listener and reconcilers work exactly as for a
subscriber-run gateway. What the owner still has to do is the daily sign-in,
and that happens inside Kopyya: the broker card's "Sign in to IBKR" button
opens their gateway's login page THROUGH the backend (``/api/ibkr-gateway/
<slot>/…``), authenticated by a short-lived token bound to their user and
slot. Their IBKR password goes straight through to IBKR; we never see it.

Slots are released (and the gateway logged out) when the account is deleted,
so a later tenant can never inherit a previous owner's session.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import timedelta
from typing import Any

import requests
import urllib3
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.security import _encode, decode_token
from app.models.broker_account import BrokerAccount

log = logging.getLogger(__name__)

PROXY_PREFIX = "/api/ibkr-gateway"
COOKIE_PREFIX = "ibkr_gw_"
LOGIN_TOKEN_MINUTES = 20
_HTTP_TIMEOUT_S = 15

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── Pool ────────────────────────────────────────────────────────────────────


def pool_size() -> int:
    try:
        return max(0, int(get_settings().ibkr_gateway_pool_size or 0))
    except (TypeError, ValueError):
        return 0


def enabled() -> bool:
    return pool_size() > 0


def gateway_url(slot: int) -> str:
    return str(get_settings().ibkr_gateway_url_template).format(slot=slot).rstrip("/")


def used_slots(db: Session) -> set[int]:
    rows = db.execute(
        select(BrokerAccount.ibkr_gateway_slot).where(BrokerAccount.ibkr_gateway_slot.is_not(None))
    ).scalars().all()
    return {int(s) for s in rows}


def free_slots(db: Session) -> list[int]:
    used = used_slots(db)
    return [s for s in range(1, pool_size() + 1) if s not in used]


def assign_slot(db: Session) -> int:
    """Lowest free slot. Caller holds the per-user broker lock; the unique
    constraint on the column is the backstop against a race between users."""
    free = free_slots(db)
    if not free:
        raise RuntimeError(
            "All hosted IBKR gateways are in use right now. Ask Kopyya support to "
            "add capacity, or connect a gateway on your own machine."
        )
    return free[0]


# ── Talking to a gateway directly (no OAuth, self-signed TLS) ───────────────


def logout(slot: int) -> bool:
    """End whatever IBKR session the gateway holds, and confirm it is gone.
    Used when a slot is released or reassigned so no session outlives its
    owner. Returns True when the gateway reports no authenticated session
    afterwards.

    The API call is ``POST /v1/api/logout``. The bare ``/logout`` the first
    version used only bounces the SSO web page (302) and left the brokerage
    session alive — on QA (2026-10-09) a subscriber who disconnected a paper
    account and reconnected for live found the paper session still signed in."""
    base = gateway_url(slot)
    for path in ("/v1/api/logout", "/logout"):
        try:
            requests.post(f"{base}{path}", data=b"", verify=False, timeout=_HTTP_TIMEOUT_S, allow_redirects=False)
        except requests.RequestException as exc:
            log.info("ibkr hosted gateway %s: %s failed (%s)", slot, path, exc)
    st = auth_status(slot)
    gone = not st["authenticated"]
    if not gone and st["reachable"]:
        log.warning("ibkr hosted gateway %s: session still authenticated after logout", slot)
    return gone


def auth_status(slot: int) -> dict[str, Any]:
    """``{"authenticated": bool, "connected": bool, "reachable": bool}``."""
    try:
        r = requests.post(
            f"{gateway_url(slot)}/v1/api/iserver/auth/status", data=b"",
            verify=False, timeout=_HTTP_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        log.info("ibkr hosted gateway %s: status failed (%s)", slot, exc)
        return {"authenticated": False, "connected": False, "reachable": False}
    if r.status_code != 200 or not r.text:
        return {"authenticated": False, "connected": False, "reachable": True}
    try:
        body = r.json()
    except ValueError:
        return {"authenticated": False, "connected": False, "reachable": True}
    return {
        "authenticated": bool(body.get("authenticated")),
        "connected": bool(body.get("connected")),
        "reachable": True,
    }


def accounts(slot: int) -> list[str]:
    """Account ids the gateway's current login can see (empty when not signed in)."""
    try:
        r = requests.get(f"{gateway_url(slot)}/v1/api/portfolio/accounts", verify=False, timeout=_HTTP_TIMEOUT_S)
        if r.status_code != 200:
            return []
        body = r.json()
    except (requests.RequestException, ValueError):
        return []
    rows = body if isinstance(body, list) else (body.get("accounts") if isinstance(body, dict) else [])
    out = []
    for a in rows or []:
        aid = (a.get("accountId") or a.get("id")) if isinstance(a, dict) else None
        if aid:
            out.append(str(aid).upper())
    return out


# ── Login-page tokens ───────────────────────────────────────────────────────


def login_token(user_id: uuid.UUID, slot: int) -> str:
    """Short-lived JWT that lets a browser navigation (no Authorization
    header possible) reach ONE slot's login page as ONE user."""
    return _encode(
        {"sub": str(user_id), "type": "ibkr_gw", "slot": int(slot)},
        timedelta(minutes=LOGIN_TOKEN_MINUTES),
    )


def verify_login_token(token: str | None, slot: int) -> uuid.UUID | None:
    if not token:
        return None
    try:
        payload = decode_token(token)
    except ValueError:
        return None
    if payload.get("type") != "ibkr_gw" or int(payload.get("slot", -1)) != int(slot):
        return None
    try:
        return uuid.UUID(str(payload.get("sub")))
    except (ValueError, TypeError):
        return None


def login_path(slot: int) -> str:
    """Where the sign-in button lands: the gateway's /sso/Login page itself.
    Not the gateway root — that needs a trailing slash, which the frontend's
    dev proxy strips, and FastAPI then answers with an absolute redirect to
    its own host/port."""
    return f"{PROXY_PREFIX}/{slot}/sso/Login"


# ── Proxy rewriting (pure functions; see api/ibkr_gateway.py) ───────────────
#
# The gateway serves its login flow with ROOT-relative paths (/sso/Login,
# /sso/Dispatcher, …) and redirects built from the request's Host. Behind the
# path prefix /api/ibkr-gateway/<slot> those would escape to the backend
# root, so redirects, cookie paths and the HTML's own links are rewritten
# onto the prefix.

_COOKIE_PATH_RE = re.compile(r"(?i);\s*Path=(/[^;]*)")
_COOKIE_DOMAIN_RE = re.compile(r"(?i);\s*Domain=[^;]*")


def rewrite_location(value: str, prefix: str, upstream: str) -> str:
    """Redirect targets: root-relative, or absolute to the upstream gateway."""
    if value.startswith(upstream):
        value = value[len(upstream):] or "/"
    if value.startswith("/") and not value.startswith("//") and not value.startswith(prefix + "/"):
        return prefix + value
    return value


def rewrite_set_cookie(value: str, prefix: str) -> str:
    """Scope the gateway's cookies to the proxy prefix and drop any Domain
    (IBKR's upstream sets Domain=.ibkr.com cookies the browser would refuse
    on our host anyway; host-only cookies work)."""
    value = _COOKIE_DOMAIN_RE.sub("", value)
    if _COOKIE_PATH_RE.search(value):
        value = _COOKIE_PATH_RE.sub(lambda m: f"; Path={prefix}{m.group(1)}", value, count=1)
    else:
        value = value + f"; Path={prefix}/"
    return value


def rewrite_body(text: str, prefix: str) -> str:
    """HTML / JS / CSS: prefix root-relative URLs. Idempotent — a value that
    already starts with the prefix is left alone."""
    esc = re.escape(prefix)
    # href="/x"  src='/x'  action="/x"  (not protocol-relative "//")
    attr_re = re.compile(rf'(?i)\b(href|src|action|formaction)=(["\'])/(?!/|{esc[1:]}/)')
    # "/sso/…"  '/v1/…'  string literals in inline JS
    str_re = re.compile(rf'(["\'])/(?!{esc[1:]}/)((?:sso|v1|demo|portal|api)/)')
    # url(/x) in CSS
    css_re = re.compile(rf'url\(\s*(["\']?)/(?!/|{esc[1:]}/)')
    text = attr_re.sub(lambda m: f"{m.group(1)}={m.group(2)}{prefix}/", text)
    text = str_re.sub(lambda m: f"{m.group(1)}{prefix}/{m.group(2)}", text)
    text = css_re.sub(lambda m: f"url({m.group(1)}{prefix}/", text)
    text = text.replace(_IBKR_SSO_BASE_EXPR, _IBKR_SSO_BASE_EXPR_PROXIED)
    return text


# IBKR's login script (sso/lib/xyz.bundle.min.js) derives the URL it posts
# credentials to from the FIRST path segment of the page:
#     origin + "/" + pathname.split("/")[1] + "/"   →   https://host/sso/
# Behind our prefix that segment is "api", so the POST would go to
# /api/Authenticator and the login fails ("Authentication failed", local
# 2026-10-08). Served through the proxy, the expression is swapped for one
# that keeps everything up to and including "sso/":
#     /api/ibkr-gateway/1/sso/Login  →  api/ibkr-gateway/1/sso/
_IBKR_SSO_BASE_EXPR = 'document.location.pathname.split("/")[1]+"/"'
_IBKR_SSO_BASE_EXPR_PROXIED = (
    'document.location.pathname.replace(/^\\/+/,"").replace(/sso\\/.*$/,"sso/")'
)


def is_rewritable(content_type: str | None) -> bool:
    """Only documents that carry URLs the browser will follow. JSON (the
    SRP/two-factor exchange) is passed through untouched."""
    ct = (content_type or "").lower()
    return any(t in ct for t in ("text/html", "javascript", "text/css"))
