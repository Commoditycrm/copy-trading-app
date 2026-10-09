"""Interactive Brokers — direct integration via IBKR's OAuth 1.0a Web API.

Each user (trader OR subscriber) creates their OWN self-service OAuth consumer
in IBKR Client Portal (Settings → API → OAuth), which hands them:

* a consumer key,
* an access token + an access token secret (the secret is RSA-encrypted
  with the user's public encryption key — only their private key opens it),
* the two private keys they generated for that consumer: a *signature* key
  and an *encryption* key,
* the Diffie-Hellman prime (``dhparam.pem``) they registered.

We store all of that Fernet-encrypted in ``broker_accounts.encrypted_credentials``::

    {
      "consumer_key":           "ABCDEFGHI",
      "access_token":           "...",
      "access_token_secret":    "<base64, RSA-encrypted>",
      "private_signature_key":  "-----BEGIN RSA PRIVATE KEY----- ...",
      "private_encryption_key": "-----BEGIN RSA PRIVATE KEY----- ...",
      "dh_prime":               "<hex>  or  -----BEGIN DH PARAMETERS----- ...",
      "account_id":             "U1234567",
      "paper":                  false,
      "realm":                  "limited_poa"      # optional
    }

How IBKR's OAuth actually works (and why requests-oauthlib can't do it)
----------------------------------------------------------------------
1. **Live Session Token (LST).** Before anything else we POST to
   ``/oauth/live_session_token`` with an OAuth header signed RSA-SHA256 by
   the private signature key, carrying a Diffie-Hellman challenge
   ``g^a mod p``. The signature base string is prefixed with the hex of the
   DECRYPTED access token secret. IBKR answers with ``g^b mod p``; the shared
   secret ``K`` feeds ``HMAC-SHA1(K, decrypted secret)`` which IS the LST.
   IBKR also returns ``HMAC-SHA1(LST, consumer_key)`` so we can verify we
   derived the same token. The LST is good for ~24h.
2. **Signed requests.** Every later call carries a standard OAuth 1.0a
   header signed HMAC-SHA256 with the LST as the key.
3. **Brokerage session.** The ``/iserver/*`` endpoints (orders, contract
   search, account) additionally need a brokerage session, opened with
   ``POST /iserver/auth/ssodh/init``. It idles out after a few minutes
   without traffic, so we re-check ``/iserver/auth/status`` once a minute
   and re-init when it has dropped.

Both the LST and the brokerage-session state live in a process-wide cache
keyed by consumer key + access token, so the listener's poll loop and the
copy engine's per-order adapters share one session instead of each
re-handshaking.

Instruments
-----------
IBKR identifies everything by ``conid``. Stocks resolve through
``/iserver/secdef/search``; options resolve underlying → ``/iserver/secdef/info``
(month + strike + right) → the row whose ``maturityDate`` matches the expiry.
Resolved conids are cached per process; contract ids are stable.

Gateway mode (individual / retail accounts)
--------------------------------------------
IBKR only grants OAuth to institutional accounts; retail logins are told to
use the **Client Portal Gateway**, a local Java app the user runs on their
own machine and signs into through a browser once a day. The gateway exposes
the SAME ``/v1/api`` endpoints without any OAuth header, so the adapter has
a second transport::

    {"mode": "gateway", "gateway_url": "https://localhost:5000",
     "account_id": "DU1234567", "paper": true}

Rules in gateway mode: no signing; the gateway's self-signed TLS certificate
is accepted; a 401 or an unauthenticated status means "nobody is logged into
the gateway", which only the user can fix in their browser; a background
``/tickle`` keeps the session from idling out between orders. The gateway
URL must point at the backend's own machine or a private network address —
this is a single-operator / self-hosted shape, never a public URL.

Operational notes
-----------------
* IBKR activates newly generated self-service OAuth keys during its nightly
  reset, so a connection attempted the same day the keys were created fails
  with 401 until the next morning. The connect form says so.
* ``paper`` is metadata: a paper account (``DU…``) uses the same host and the
  same OAuth material as the live one.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlparse

import requests
import urllib3
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.models.order import (
    InstrumentType,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
)

log = logging.getLogger(__name__)


BASE_URL = "https://api.ibkr.com/v1/api"
LST_PATH = "/oauth/live_session_token"
# Self-service (first-party) consumers sign into the "limited_poa" realm;
# IBKR's shared TESTCONS key uses "test_realm". Overridable per account.
DEFAULT_REALM = "limited_poa"
_USER_AGENT = "copy-trader/1.0 (ibkr-oauth1a)"
_DH_GENERATOR = 2
# Re-handshake this long before the LST's stated expiry so a token never
# dies mid-poll.
_LST_REFRESH_MARGIN_S = 3600
# How long an "authenticated" answer from /iserver/auth/status is trusted
# before we ask again. Every signed call keeps the session alive, so this
# only matters for adapters that go quiet (a subscriber between mirrors).
_BROKERAGE_STATUS_INTERVAL_S = 60.0
_HTTP_TIMEOUT_S = 20

# IBKR order status → our enum. IBKR is inconsistent across endpoints
# (some endpoints return "PreSubmitted", others "PRESUBMITTED", others
# "Pre Submitted") so we normalise to UPPER_SNAKE before lookup.
_STATUS_IN: dict[str, OrderStatus] = {
    "PENDINGSUBMIT":    OrderStatus.PENDING,
    "PENDING_SUBMIT":   OrderStatus.PENDING,
    "PRESUBMITTED":     OrderStatus.SUBMITTED,
    "PRE_SUBMITTED":    OrderStatus.SUBMITTED,
    "SUBMITTED":        OrderStatus.SUBMITTED,
    "ACCEPTED":         OrderStatus.ACCEPTED,
    "FILLED":           OrderStatus.FILLED,
    "PARTIALLYFILLED":  OrderStatus.PARTIALLY_FILLED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "CANCELLED":        OrderStatus.CANCELED,
    "CANCELED":         OrderStatus.CANCELED,
    "REJECTED":         OrderStatus.REJECTED,
    "INACTIVE":         OrderStatus.REJECTED,
    "EXPIRED":          OrderStatus.EXPIRED,
}

# Our → IBKR placement enums.
_SIDE_OUT = {OrderSide.BUY: "BUY", OrderSide.SELL: "SELL"}
_TYPE_OUT = {
    OrderType.MARKET:     "MKT",
    OrderType.LIMIT:      "LMT",
    OrderType.STOP:       "STP",
    OrderType.STOP_LIMIT: "STP_LMT",
}
_RIGHT_OUT = {OptionRight.CALL: "C", OptionRight.PUT: "P"}


# ── Small helpers ───────────────────────────────────────────────────────────


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Tolerant lookup — IBKR responses are mostly plain dicts, but field
    names vary between endpoints (orderId vs order_id vs id)."""
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _to_dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _norm_status(raw: Any) -> str:
    return str(raw or "SUBMITTED").upper().replace(" ", "_")


def _fmt_strike(strike: Decimal) -> str:
    """``Decimal('450.00')`` → ``'450'``, ``Decimal('452.50')`` → ``'452.5'``.
    IBKR matches strikes textually in ``/secdef/info``."""
    s = format(strike.normalize(), "f")
    return s


def build_occ_symbol(symbol: str, expiry: date, strike: Decimal, right: OptionRight) -> str:
    """OCC 21-char symbol with no inner padding (``AAPL250719C00200000``) — the
    same form the Alpaca and Webull adapters use for ``broker_symbol``."""
    cp = "C" if right == OptionRight.CALL else "P"
    return f"{symbol.upper()}{expiry.strftime('%y%m%d')}{cp}{int(strike * 1000):08d}"


_OCC_RE = re.compile(r"^([A-Z.]{1,6})\s*(\d{6})([CP])(\d{8})$")
# "AAPL 06JUN26 200 C"  — IBKR's compact contractDesc.
_DESC_COMPACT_RE = re.compile(
    r"^([A-Z.]{1,6})\s+(\d{2})([A-Z]{3})(\d{2})\s+(\d+(?:\.\d+)?)\s+([CP])(?:ALL|UT)?$"
)
# "SPY DEC 19 '25 600 Call" — the TWS-style description seen on some rows.
_DESC_TWS_RE = re.compile(
    r"^([A-Z.]{1,6})\s+([A-Z]{3})\s+(\d{1,2})\s+'(\d{2})\s+(\d+(?:\.\d+)?)\s+(C|P|CALL|PUT)$"
)
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1
)}


def parse_contract_desc(desc: str | None) -> tuple[str, date, Decimal, OptionRight] | None:
    """Best-effort parse of an IBKR option description into
    ``(underlying, expiry, strike, right)``. Handles the compact
    ``AAPL 06JUN26 200 C`` form, the TWS ``SPY DEC 19 '25 600 Call`` form and
    a (possibly space-padded) OCC symbol. ``None`` when it matches nothing —
    callers then fall back to the authoritative ``/iserver/contract/{conid}/info``."""
    if not desc:
        return None
    s = " ".join(str(desc).upper().split())
    m = _OCC_RE.match(s.replace(" ", "")) or _OCC_RE.match(s)
    if m:
        root, yymmdd, cp, strike_str = m.groups()
        try:
            expiry = date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
        except ValueError:
            return None
        return root, expiry, Decimal(strike_str) / 1000, (
            OptionRight.CALL if cp == "C" else OptionRight.PUT
        )
    m = _DESC_COMPACT_RE.match(s)
    if m:
        root, dd, mon, yy, strike_str, cp = m.groups()
        try:
            expiry = date(2000 + int(yy), _MONTHS[mon], int(dd))
        except (KeyError, ValueError):
            return None
        return root, expiry, Decimal(strike_str), (
            OptionRight.CALL if cp == "C" else OptionRight.PUT
        )
    m = _DESC_TWS_RE.match(s)
    if m:
        root, mon, dd, yy, strike_str, cp = m.groups()
        try:
            expiry = date(2000 + int(yy), _MONTHS[mon], int(dd))
        except (KeyError, ValueError):
            return None
        return root, expiry, Decimal(strike_str), (
            OptionRight.CALL if cp.startswith("C") else OptionRight.PUT
        )
    return None


# ── OAuth primitives ────────────────────────────────────────────────────────


def _pct(s: str) -> str:
    """RFC 3986 percent-encoding as OAuth 1.0a wants it (nothing but
    unreserved characters survive)."""
    return quote(str(s), safe="")


def _base_string(method: str, url: str, params: dict[str, str]) -> str:
    """OAuth 1.0a signature base string: ``METHOD&url&k1=v1&k2=v2`` with the
    parameters sorted and the url + parameter string each percent-encoded.
    ``params`` must NOT contain ``oauth_signature``."""
    pairs = sorted((_pct(k), _pct(v)) for k, v in params.items())
    param_str = "&".join(f"{k}={v}" for k, v in pairs)
    return f"{method.upper()}&{_pct(url)}&{_pct(param_str)}"


def _auth_header(realm: str, params: dict[str, str]) -> str:
    """``OAuth realm="…", k="v", …`` — values are already percent-encoded
    where they need to be (the signature), everything else is url-safe."""
    body = ", ".join(f'{k}="{v}"' for k, v in sorted(params.items()))
    return f'OAuth realm="{realm}", {body}'


def _load_private_key(pem: str, what: str) -> rsa.RSAPrivateKey:
    """Accept a PEM (PKCS#1 or PKCS#8) pasted with real newlines, with literal
    ``\\n`` escapes, or as a bare base64 body without the BEGIN/END lines."""
    text = str(pem or "").strip().replace("\\n", "\n")
    if "-----BEGIN" not in text:
        body = "".join(text.split())
        text = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
    try:
        key = serialization.load_pem_private_key(text.encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"IBKR {what} is not a readable PEM private key: {exc}") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise RuntimeError(f"IBKR {what} must be an RSA key")
    return key


def _parse_dh_prime(value: str) -> int:
    """The DH prime either as the hex string IBKR shows, or the
    ``dhparam.pem`` the user generated (we read ``p`` out of it)."""
    text = str(value or "").strip().replace("\\n", "\n")
    if "-----BEGIN" in text:
        try:
            params = serialization.load_pem_parameters(text.encode())
            return int(params.parameter_numbers().p)
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError(f"IBKR DH parameters PEM is unreadable: {exc}") from exc
    hex_str = "".join(text.split()).lower()
    if hex_str.startswith("0x"):
        hex_str = hex_str[2:]
    if not hex_str or not re.fullmatch(r"[0-9a-f]+", hex_str):
        raise RuntimeError("IBKR DH prime must be a hex string or a DH PARAMETERS PEM")
    p = int(hex_str, 16)
    if p < (1 << 500):
        raise RuntimeError("IBKR DH prime is too small to be the registered modulus")
    return p


def _int_to_dh_bytes(k: int) -> bytes:
    """Big-endian bytes of ``k`` with a leading zero byte when the top bit is
    set — IBKR derives the LST from Java's two's-complement BigInteger
    encoding, so we must match it byte for byte."""
    hex_str = format(k, "x")
    if len(hex_str) % 2:
        hex_str = "0" + hex_str
    raw = bytes.fromhex(hex_str)
    if raw and raw[0] & 0x80:
        raw = b"\x00" + raw
    return raw


@dataclass
class _Session:
    """Process-wide auth state for ONE (consumer key, access token) pair."""
    lock: threading.RLock = field(default_factory=threading.RLock)
    lst: bytes | None = None            # the live session token, base64-decoded
    lst_expires_at: float = 0.0         # epoch seconds
    brokerage_checked_at: float = 0.0   # last time /iserver/auth/status was "authenticated"
    portfolio_primed: bool = False      # /portfolio/accounts called this session


_SESSIONS: dict[str, _Session] = {}
_SESSIONS_LOCK = threading.Lock()


def _session_for(consumer_key: str, access_token: str) -> _Session:
    key = hashlib.sha256(f"{consumer_key}:{access_token}".encode()).hexdigest()
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(key)
        if s is None:
            s = _SESSIONS[key] = _Session()
        return s


class IBKRAuthError(RuntimeError):
    """Credentials rejected (401) even after a fresh handshake — or, in
    gateway mode, nobody is logged into the Client Portal Gateway."""


DEFAULT_GATEWAY_URL = "https://localhost:5000"
_GATEWAY_TICKLE_INTERVAL_S = 60.0
_GATEWAY_LOCAL_SUFFIXES = (".localhost", ".local", ".ts.net", ".internal", ".lan")


def normalize_gateway_url(raw: str | None) -> str:
    """Validate a Client Portal Gateway origin. The backend will send
    requests to it, so it must be this machine or a private network: an SSRF
    guard, and also simply how the gateway works (IBKR only serves API calls
    from the machine where the browser login happened)."""
    text = (raw or DEFAULT_GATEWAY_URL).strip().rstrip("/")
    if "://" not in text:
        text = "https://" + text
    u = urlparse(text)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise RuntimeError(f"IBKR gateway URL is not a valid http(s) origin: {raw!r}")
    if u.path not in ("", "/") or u.query or u.fragment:
        raise RuntimeError("IBKR gateway URL must be just the origin, e.g. https://localhost:5000")
    host = u.hostname.lower()
    ok = host == "localhost" or host.endswith(_GATEWAY_LOCAL_SUFFIXES)
    if not ok:
        ok = _is_private_host(host)
    if not ok:
        raise RuntimeError(
            "IBKR gateway URL must be localhost or a private-network address — "
            "the Client Portal Gateway runs on your own machine"
        )
    return f"{u.scheme}://{u.hostname}{':' + str(u.port) if u.port else ''}"


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_private or ip.is_loopback or ip.is_link_local or (
        ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")  # CGNAT / Tailscale
    )


def _is_private_host(host: str) -> bool:
    """An IP literal on a private range, or a name (a compose service such as
    ``ibkr-gw-1``) that resolves ONLY to private addresses. Resolution failing
    counts as not private."""
    try:
        return _is_private_ip(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    addrs = {ipaddress.ip_address(i[4][0]) for i in infos}
    return bool(addrs) and all(_is_private_ip(a) for a in addrs)


_gateway_keepalives: set[str] = set()
_gateway_keepalive_lock = threading.Lock()


def _start_gateway_keepalive(api_base: str) -> None:
    """One daemon thread per gateway that GETs ``/tickle`` every minute so the
    brokerage session survives idle stretches (it times out after ~6 min).
    Failures are logged and retried; the thread lives as long as the process."""
    with _gateway_keepalive_lock:
        if api_base in _gateway_keepalives:
            return
        _gateway_keepalives.add(api_base)

    def _loop() -> None:
        while True:
            time.sleep(_GATEWAY_TICKLE_INTERVAL_S)
            try:
                requests.post(
                    f"{api_base}/tickle", headers={"User-Agent": _USER_AGENT},
                    timeout=_HTTP_TIMEOUT_S, verify=False,
                )
            except requests.RequestException as exc:
                log.debug("ibkr gateway tickle failed (%s): %s", api_base, exc)

    threading.Thread(target=_loop, name=f"ibkr-gateway-tickle", daemon=True).start()


# ── Adapter ─────────────────────────────────────────────────────────────────


class IBKRAdapter(BrokerAdapter):
    """OAuth Web API client for ONE user's IBKR account. See the module
    docstring for the auth flow."""

    name = "ibkr"
    # IBKR has no native "replace"; the copy engine cancels and re-places.
    supports_replace = False
    # A MARKET order outside regular hours just queues until the open;
    # extended-hours mirrors are re-routed as outsideRTH limits.
    requires_extended_hours_limit = True

    # Process-wide caches. Contract ids are stable so sharing is safe.
    _conid_cache: dict[str, int] = {}
    _option_detail_cache: dict[int, tuple[str, date, Decimal, OptionRight]] = {}

    def __init__(self, credentials: dict[str, Any]):
        super().__init__(credentials)
        self._paper = bool(credentials.get("paper", False))
        mode = str(credentials.get("mode") or ("gateway" if credentials.get("gateway_url") else "oauth"))
        self._gateway = mode == "gateway"
        if self._gateway:
            try:
                self._account_id = str(credentials["account_id"]).strip().upper()
            except KeyError as exc:
                raise RuntimeError("IBKR credentials missing 'account_id'") from exc
            self._gateway_url = normalize_gateway_url(credentials.get("gateway_url"))
            self._hosted = bool(credentials.get("hosted"))
            self._base_url = self._gateway_url + "/v1/api"
            self._realm = DEFAULT_REALM
            self._session = _session_for("gateway", self._base_url)
            # The gateway ships a self-signed certificate for localhost.
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            _start_gateway_keepalive(self._base_url)
            return
        self._base_url = BASE_URL
        if "private_signature_key" not in credentials and "signing_key" in credentials:
            raise RuntimeError(
                "This IBKR connection was saved with the old single-signing-key "
                "form, which IBKR's OAuth never accepted. Disconnect it and "
                "reconnect with the consumer key, access token + secret, your "
                "private signature and encryption keys, and the DH prime."
            )
        try:
            self._consumer_key = str(credentials["consumer_key"]).strip()
            self._access_token = str(credentials["access_token"]).strip()
            self._access_token_secret = "".join(str(credentials["access_token_secret"]).split())
            self._account_id = str(credentials["account_id"]).strip().upper()
            self._sig_key = _load_private_key(
                credentials["private_signature_key"], "private signature key"
            )
            self._enc_key = _load_private_key(
                credentials["private_encryption_key"], "private encryption key"
            )
            self._dh_prime = _parse_dh_prime(credentials["dh_prime"])
        except KeyError as exc:
            raise RuntimeError(f"IBKR credentials missing {exc.args[0]!r}") from exc
        self._realm = str(credentials.get("realm") or DEFAULT_REALM)
        self._session = _session_for(self._consumer_key, self._access_token)

    # ── Live Session Token ────────────────────────────────────────────────

    def _decrypted_token_secret(self) -> bytes:
        try:
            raw = base64.b64decode(self._access_token_secret)
            return self._enc_key.decrypt(raw, padding.PKCS1v15())
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "IBKR access token secret could not be decrypted with the private "
                "encryption key — make sure both come from the same OAuth consumer"
            ) from exc

    def _lst(self) -> bytes:
        """The current live session token, handshaking when missing or about
        to expire. Serialised per session so concurrent callers share one
        handshake."""
        if self._gateway:
            raise RuntimeError("IBKR gateway mode has no live session token")
        s = self._session
        with s.lock:
            if s.lst is None or time.time() > s.lst_expires_at - _LST_REFRESH_MARGIN_S:
                self._handshake()
            assert s.lst is not None
            return s.lst

    def _handshake(self) -> None:
        prepend = self._decrypted_token_secret()
        a = secrets.randbits(256)
        challenge = format(pow(_DH_GENERATOR, a, self._dh_prime), "x")
        url = BASE_URL + LST_PATH
        params = {
            "oauth_consumer_key": self._consumer_key,
            "oauth_nonce": secrets.token_hex(16),
            "oauth_signature_method": "RSA-SHA256",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self._access_token,
            "diffie_hellman_challenge": challenge,
        }
        base = prepend.hex() + _base_string("POST", url, params)
        signature = self._sig_key.sign(base.encode(), padding.PKCS1v15(), hashes.SHA256())
        params["oauth_signature"] = _pct(base64.b64encode(signature).decode())
        headers = {
            "Authorization": _auth_header(self._realm, params),
            "Accept": "*/*",
            "User-Agent": _USER_AGENT,
        }
        try:
            r = requests.post(url, headers=headers, timeout=_HTTP_TIMEOUT_S)
        except requests.RequestException as exc:
            raise RuntimeError(f"IBKR network error during handshake: {exc}") from exc
        if r.status_code != 200:
            raise IBKRAuthError(
                f"IBKR rejected the live-session-token request (HTTP {r.status_code}). "
                "Check the consumer key, access token + secret, private keys and DH "
                "prime all belong to the same OAuth consumer, and note that keys "
                "created today only activate after IBKR's nightly reset. "
                f"body={r.text[:300]!r}"
            )
        try:
            body = r.json()
            dh_response = str(body["diffie_hellman_response"])
            lst_signature = str(body["live_session_token_signature"]).lower()
            expires_ms = body.get("live_session_token_expiration")
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"IBKR handshake returned an unexpected body: {r.text[:300]!r}") from exc

        shared = pow(int(dh_response, 16), a, self._dh_prime)
        lst = hmac.new(_int_to_dh_bytes(shared), prepend, hashlib.sha1).digest()
        expected = hmac.new(lst, self._consumer_key.encode(), hashlib.sha1).hexdigest()
        if not hmac.compare_digest(expected, lst_signature):
            raise IBKRAuthError(
                "IBKR live session token failed verification — the DH prime or "
                "private encryption key doesn't match what the consumer key was "
                "registered with."
            )
        s = self._session
        s.lst = lst
        s.lst_expires_at = (
            float(expires_ms) / 1000.0 if expires_ms else time.time() + 23 * 3600
        )
        s.brokerage_checked_at = 0.0
        s.portfolio_primed = False
        log.info("ibkr: live session token established for consumer %s…", self._consumer_key[:3])

    def _invalidate_lst(self) -> None:
        s = self._session
        with s.lock:
            s.lst = None
            s.lst_expires_at = 0.0
            s.brokerage_checked_at = 0.0
            s.portfolio_primed = False

    # ── Signed HTTP ───────────────────────────────────────────────────────

    def _http(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> tuple[int, Any]:
        """One call — OAuth-signed against IBKR's hosted API, or plain against
        a local Client Portal Gateway. Returns ``(status_code, parsed_body)``
        and leaves auth/retry decisions to the caller."""
        url = self._base_url + path
        query = {k: str(v) for k, v in (params or {}).items() if v is not None}
        headers = {"Accept": "*/*", "User-Agent": _USER_AGENT}
        if not self._gateway:
            oauth = {
                "oauth_consumer_key": self._consumer_key,
                "oauth_nonce": secrets.token_hex(16),
                "oauth_signature_method": "HMAC-SHA256",
                "oauth_timestamp": str(int(time.time())),
                "oauth_token": self._access_token,
            }
            base = _base_string(method, url, {**query, **oauth})
            sig = hmac.new(self._lst(), base.encode(), hashlib.sha256).digest()
            oauth["oauth_signature"] = _pct(base64.b64encode(sig).decode())
            headers["Authorization"] = _auth_header(self._realm, oauth)
        try:
            r = requests.request(
                method, url, params=query or None, json=json,
                headers=headers, timeout=_HTTP_TIMEOUT_S,
                verify=not self._gateway,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"IBKR network error: {exc}") from exc
        if not r.text:
            return r.status_code, {}
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text

    @staticmethod
    def _says_not_authenticated(body: Any) -> bool:
        if isinstance(body, dict):
            err = str(body.get("error") or body.get("message") or "").lower()
            return "not authenticated" in err or ("session" in err and "expired" in err)
        if isinstance(body, str):
            return "not authenticated" in body.lower()
        return False

    def _ensure_brokerage_session(self, *, force: bool = False) -> None:
        """Make sure an ``/iserver`` brokerage session is open. Cheap when one
        was confirmed within the last minute."""
        s = self._session
        with s.lock:
            if not force and time.time() - s.brokerage_checked_at < _BROKERAGE_STATUS_INTERVAL_S:
                return
            code, status = self._http("POST", "/iserver/auth/status")
            if code == 200 and isinstance(status, dict) and status.get("authenticated"):
                s.brokerage_checked_at = time.time()
                return
            if self._gateway and (
                code == 401 or not (isinstance(status, dict) and status.get("connected"))
            ):
                raise IBKRAuthError(self._gateway_login_message(status))
            code, body = self._http(
                "POST", "/iserver/auth/ssodh/init", json={"publish": True, "compete": True},
            )
            if code == 401:
                if self._gateway:
                    raise IBKRAuthError(self._gateway_login_message(body))
                raise IBKRAuthError(f"IBKR brokerage session init rejected (401): {body!r}"[:400])
            if isinstance(body, dict) and body.get("authenticated"):
                s.brokerage_checked_at = time.time()
                return
            for _ in range(6):
                time.sleep(1.0)
                code, status = self._http("POST", "/iserver/auth/status")
                if code == 200 and isinstance(status, dict) and status.get("authenticated"):
                    s.brokerage_checked_at = time.time()
                    return
            if self._gateway:
                raise IBKRAuthError(self._gateway_login_message(status))
            raise RuntimeError(
                "IBKR brokerage session did not authenticate after ssodh/init. "
                f"last status={status!r}"[:400]
            )

    def _gateway_login_message(self, body: Any = None) -> str:
        if getattr(self, "_hosted", False):
            return (
                "Your IBKR session isn't signed in. Open Kopyya → Broker and click "
                "\"Sign in to IBKR\" on your IBKR card, then retry. (IBKR ends every "
                "session at midnight New York time.)"
            )
        return (
            f"IBKR Client Portal Gateway at {self._gateway_url} has no logged-in "
            f"session. Open {self._gateway_url} in a browser on that machine, sign in "
            "with the IBKR username for this account, then retry. "
            f"(gateway said: {str(body)[:160]!r})"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        """Signed call with the session plumbing: brokerage session for
        ``/iserver`` and ``/portfolio``, one re-init on "not authenticated",
        one full re-handshake on 401."""
        needs_brokerage = path.startswith("/iserver") or path.startswith("/portfolio")
        if needs_brokerage:
            self._ensure_brokerage_session()
        code, body = self._http(method, path, params=params, json=json)

        if code == 401 and self._gateway:
            # Only a browser login fixes this; don't spin.
            self._session.brokerage_checked_at = 0.0
            raise IBKRAuthError(self._gateway_login_message(body))
        if code == 401:
            log.info("ibkr: 401 on %s %s — re-handshaking once", method, path)
            self._invalidate_lst()
            if needs_brokerage:
                self._ensure_brokerage_session(force=True)
            code, body = self._http(method, path, params=params, json=json)
            if code == 401:
                raise IBKRAuthError(
                    f"IBKR auth rejected (401) on {method} {path} even after a fresh "
                    f"handshake — the access token may have been revoked. body={body!r}"[:400]
                )
        elif needs_brokerage and self._says_not_authenticated(body):
            log.info("ibkr: brokerage session dropped on %s %s — re-initialising", method, path)
            self._ensure_brokerage_session(force=True)
            code, body = self._http(method, path, params=params, json=json)

        if code >= 400:
            raise RuntimeError(f"IBKR {method} {path}: HTTP {code} — {str(body)[:400]}")
        if isinstance(body, dict) and body.get("error") and len(body) <= 2:
            # IBKR reports many failures as 200 + {"error": "..."}.
            raise RuntimeError(f"IBKR {method} {path}: {body['error']}")
        return body

    def _portfolio_request(self, method: str, path: str, **kw: Any) -> Any:
        """``/portfolio/{acct}/*`` endpoints need ``/portfolio/accounts`` to have
        been called once in the session; do that lazily."""
        s = self._session
        if not s.portfolio_primed:
            self._request("GET", "/portfolio/accounts")
            s.portfolio_primed = True
        return self._request(method, path, **kw)

    # ── Account info / verify ─────────────────────────────────────────────

    def verify_connection(self) -> ConnectionInfo:
        """Full handshake (OAuth) or gateway login check, then the brokerage
        session and the list of accounts this login can see. If our stored
        ``account_id`` isn't among them, surface a clean message so the user
        can fix the form instead of having every subsequent order fail
        mysteriously."""
        if not self._gateway:
            self._lst()
        body = self._request("GET", "/portfolio/accounts")
        self._session.portfolio_primed = True
        accounts = body if isinstance(body, list) else (
            body.get("accounts") if isinstance(body, dict) else []
        )
        account_ids = {
            str(_attr(a, "accountId", "id") or "").upper()
            for a in (accounts or []) if a
        } - {""}
        if account_ids and self._account_id not in account_ids:
            raise RuntimeError(
                f"IBKR auth succeeded but account_id '{self._account_id}' "
                f"isn't in the connected accounts ({sorted(account_ids)}). "
                "Re-check the account number on the connect form."
            )
        return ConnectionInfo(
            broker_account_id=self._account_id,
            # Fractional-share support is symbol-specific and gated by
            # account permissions; default off.
            supports_fractional=False,
            extra={
                "paper": self._paper,
                "accounts": sorted(account_ids),
                "mode": "gateway" if self._gateway else "oauth",
                **({"gateway_url": self._gateway_url} if self._gateway else {}),
            },
        )

    # ── Contract resolution ───────────────────────────────────────────────

    def _conid_for(self, symbol: str) -> int:
        """Stock (also an option's underlying) → conid."""
        sym = symbol.upper().strip()
        if sym in self._conid_cache:
            return self._conid_cache[sym]
        body = self._request(
            "GET", "/iserver/secdef/search", params={"symbol": sym, "secType": "STK"},
        )
        if not isinstance(body, list) or not body:
            raise RuntimeError(f"IBKR symbol lookup empty for '{sym}'")
        best = next(
            (h for h in body
             if (_attr(h, "secType") or "STK").upper() == "STK"
             and (_attr(h, "symbol") or "").upper() == sym),
            body[0],
        )
        conid = _attr(best, "conid", "conId")
        try:
            conid_int = int(conid)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"IBKR symbol lookup for '{sym}' returned no usable conid: {best!r}") from exc
        self._conid_cache[sym] = conid_int
        return conid_int

    def _option_conid(
        self, symbol: str, expiry: date, strike: Decimal, right: OptionRight,
    ) -> int:
        """Option contract → conid via ``/iserver/secdef/info`` on the
        underlying, filtered to the exact expiry."""
        occ = build_occ_symbol(symbol, expiry, strike, right)
        if occ in self._conid_cache:
            return self._conid_cache[occ]
        underlying = self._conid_for(symbol)
        month = expiry.strftime("%b%y").upper()          # OCT26
        want_maturity = expiry.strftime("%Y%m%d")       # 20261017
        rows = self._request(
            "GET", "/iserver/secdef/info",
            params={
                "conid": underlying,
                "sectype": "OPT",
                "month": month,
                "strike": _fmt_strike(strike),
                "right": _RIGHT_OUT[right],
                "exchange": "SMART",
            },
        )
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            rows = []
        match = None
        for r in rows:
            if str(_attr(r, "maturityDate", "maturity_date") or "") != want_maturity:
                continue
            r_right = str(_attr(r, "right") or _RIGHT_OUT[right]).upper()[:1]
            if r_right != _RIGHT_OUT[right]:
                continue
            r_strike = _to_dec(_attr(r, "strike"))
            if r_strike is not None and r_strike != strike:
                continue
            match = r
            break
        if match is None:
            raise RuntimeError(
                f"IBKR has no {symbol.upper()} {expiry.isoformat()} "
                f"{_fmt_strike(strike)}{_RIGHT_OUT[right]} contract "
                f"({len(rows)} candidate(s) for {month})"
            )
        try:
            conid = int(_attr(match, "conid", "conId"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"IBKR option lookup returned no usable conid: {match!r}") from exc
        self._conid_cache[occ] = conid
        self._option_detail_cache[conid] = (symbol.upper(), expiry, strike, right)
        return conid

    def option_details(self, conid: int | str) -> tuple[str, date, Decimal, OptionRight] | None:
        """``(underlying, expiry, strike, right)`` for an option conid, from
        ``/iserver/contract/{conid}/info``. Cached. ``None`` if IBKR's answer
        can't be read — never raises, so a listener poll survives it."""
        try:
            cid = int(conid)
        except (TypeError, ValueError):
            return None
        if cid in self._option_detail_cache:
            return self._option_detail_cache[cid]
        try:
            info = self._request("GET", f"/iserver/contract/{cid}/info")
        except Exception as exc:  # noqa: BLE001
            log.warning("ibkr: contract info for conid %s failed: %s", cid, exc)
            return None
        parsed = parse_contract_desc(_attr(info, "local_symbol", "localSymbol"))
        if parsed is None:
            maturity = str(_attr(info, "maturity_date", "maturityDate") or "")
            strike = _to_dec(_attr(info, "strike"))
            right_raw = str(_attr(info, "right") or "").upper()[:1]
            symbol = str(_attr(info, "symbol", "underlying_symbol") or "").upper()
            if len(maturity) == 8 and strike is not None and right_raw in ("C", "P") and symbol:
                try:
                    expiry = date(int(maturity[:4]), int(maturity[4:6]), int(maturity[6:8]))
                except ValueError:
                    expiry = None
                if expiry is not None:
                    parsed = (
                        symbol, expiry, strike,
                        OptionRight.CALL if right_raw == "C" else OptionRight.PUT,
                    )
        if parsed is None:
            log.warning("ibkr: could not read option contract %s: %r", cid, info)
            return None
        self._option_detail_cache[cid] = parsed
        return parsed

    # ── Orders ────────────────────────────────────────────────────────────

    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        if req.order_type not in _TYPE_OUT:
            raise ValueError(f"IBKR adapter: order type {req.order_type.value} is not supported")
        if req.take_profit_price is not None or req.stop_loss_price is not None:
            log.warning(
                "ibkr: native bracket legs not supported — placing %s %s as a plain "
                "order; the bracket emulator covers the exits", req.side.value, req.symbol,
            )

        if req.instrument_type == InstrumentType.OPTION:
            if req.option_expiry is None or req.option_strike is None or req.option_right is None:
                raise ValueError("IBKR adapter: option order needs expiry, strike and right")
            conid = self._option_conid(
                req.symbol, req.option_expiry, req.option_strike, req.option_right,
            )
            sec_type = f"{conid}:OPT"
        else:
            conid = self._conid_for(req.symbol)
            sec_type = f"{conid}:STK"

        qty = req.quantity
        order: dict[str, Any] = {
            "acctId":    self._account_id,
            "conid":     conid,
            "secType":   sec_type,
            "orderType": _TYPE_OUT[req.order_type],
            "side":      _SIDE_OUT[req.side],
            "quantity":  int(qty) if qty == qty.to_integral_value() else float(qty),
            "tif":       "DAY",
        }
        if req.limit_price is not None:
            order["price"] = float(req.limit_price)
        if req.stop_price is not None:
            order["auxPrice"] = float(req.stop_price)
        if req.extended_hours and req.instrument_type == InstrumentType.STOCK:
            order["outsideRTH"] = True
        if req.client_order_id:
            # IBKR echoes this back as ``order_ref`` on the orders feed, cut to
            # 32 characters. A dashed UUID is 36, so send the 32-char hex form —
            # it survives intact and uuid.UUID() parses it on the way back.
            cid = str(req.client_order_id).strip()
            try:
                cid = uuid.UUID(cid).hex
            except ValueError:
                cid = cid[:32]
            order["cOID"] = cid

        body = self._request(
            "POST",
            f"/iserver/account/{self._account_id}/orders",
            json={"orders": [order]},
        )
        # IBKR returns a list. Each item is either the placed order (with
        # ``order_id`` + ``order_status``) OR a confirmation prompt with an
        # ``id`` we must POST to /iserver/reply/{id}. Loop a few times to
        # clear any "Are you sure?" prompts before giving up.
        for _ in range(5):
            if isinstance(body, dict):
                body = [body]
            if not isinstance(body, list) or not body:
                raise RuntimeError(f"IBKR place_order: unexpected response {body!r}")
            first = body[0]
            if isinstance(first, dict) and first.get("error"):
                raise RuntimeError(f"IBKR place_order rejected: {first['error']}")
            order_status = _attr(first, "order_status", "orderStatus", "status")
            broker_order_id = _attr(first, "order_id", "orderId")
            if broker_order_id is None and _attr(first, "id"):
                body = self._request(
                    "POST",
                    f"/iserver/reply/{_attr(first, 'id')}",
                    json={"confirmed": True},
                )
                continue
            if not broker_order_id:
                raise RuntimeError(f"IBKR place_order returned no order id: {first!r}")
            return BrokerOrderResult(
                broker_order_id=str(broker_order_id),
                status=_STATUS_IN.get(_norm_status(order_status), OrderStatus.SUBMITTED),
                submitted_at=datetime.now(timezone.utc),
                filled_quantity=Decimal(0),
                filled_avg_price=None,
            )
        raise RuntimeError("IBKR place_order: confirmation loop exceeded (5 prompts)")

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        """IBKR has no clean get-by-id; we scan the recent-orders feed
        (same source the listener uses)."""
        for o in self.list_recent_activities():
            if str(_attr(o, "orderId", "order_id", "id") or "") == str(broker_order_id):
                return self._order_to_result(o)
        raise LookupError(f"IBKR order {broker_order_id} not in recent orders feed")

    _TERMINAL = (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED)

    def cancel_order(self, broker_order_id: str) -> bool:
        """True when the order is cancelled at IBKR after this call; False when
        IBKR had ALREADY finished with it (filled / cancelled earlier); raises
        only when its state is unknown. See base.cancel_order.

        IBKR's DELETE is not a clean request/response: the paper gateway has
        answered **503 Service Unavailable while actually performing the
        cancel** (2026-10-07, twice), and a second attempt then gets "Cancel
        attempted when order is not in a cancellable state" or "OrderID …
        doesn't exist". So every failure is settled by reading the order back:
        the orders feed, not the DELETE's status code, is the truth."""
        try:
            self._request(
                "DELETE",
                f"/iserver/account/{self._account_id}/order/{broker_order_id}",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            # A 4xx that says the order was already finished with → the DELETE
            # did nothing; a 5xx / network failure → it may well have done it.
            already_dead = any(k in msg for k in (
                "doesn't exist", "does not exist", "not found", "not in a cancellable state",
            ))
            status = self._settled_status_after_cancel(broker_order_id)
            if status == OrderStatus.CANCELED and not already_dead:
                log.info("ibkr: cancel of %s errored (%s) but the order is cancelled", broker_order_id, exc)
                return True
            if status in self._TERMINAL:
                log.info("ibkr: cancel of %s — already %s at IBKR, nothing to cancel", broker_order_id, status.value)
                return False
            raise RuntimeError(f"IBKR cancel_order: {exc}") from exc

    def _settled_status_after_cancel(self, broker_order_id: str) -> OrderStatus | None:
        """Read the order back a few times — a cancel acknowledged with a 503 can
        take a moment to show as Cancelled on the feed."""
        last: OrderStatus | None = None
        for attempt in range(4):
            try:
                last = self.get_order(broker_order_id).status
            except Exception:  # noqa: BLE001
                last = None
            if last in self._TERMINAL:
                return last
            if attempt < 3:
                time.sleep(0.75)
        return last

    # ── Balances / P&L ────────────────────────────────────────────────────

    @staticmethod
    def _summary_amount(summary: Any, key: str) -> Decimal | None:
        """``/portfolio/{acct}/summary`` nests each figure as
        ``{"amount": 1031939.06, "currency": "USD", ...}``."""
        cell = summary.get(key) if isinstance(summary, dict) else None
        if isinstance(cell, dict):
            if cell.get("isNull"):
                return None
            return _to_dec(cell.get("amount") if cell.get("amount") is not None else cell.get("value"))
        return _to_dec(cell)

    def get_balance_snapshot(self) -> dict[str, Any]:
        """Cash / buying power / equity for the Brokers card and connect. Same
        shape as the Alpaca / SnapTrade / Webull adapters so
        ``balance_sync.refresh_account_balance`` can consume it."""
        summary = self._portfolio_request("GET", f"/portfolio/{self._account_id}/summary")
        if not isinstance(summary, dict) or not summary:
            raise RuntimeError("IBKR account summary came back empty")
        equity = self._summary_amount(summary, "netliquidation")
        cash = self._summary_amount(summary, "totalcashvalue")
        bp = self._summary_amount(summary, "buyingpower") or self._summary_amount(summary, "availablefunds")
        ccy = None
        cell = summary.get("netliquidation")
        if isinstance(cell, dict):
            ccy = cell.get("currency")
        return {
            "cash": cash,
            "buying_power": bp,
            "total_equity": equity,
            "currency": ccy or "USD",
        }

    def get_pnl_snapshot(self) -> dict[str, Any] | None:
        """Equity / day-start / today's P&L for the daily kill switches, from
        ``/iserver/account/pnl/partitioned`` (``dpl`` = day P&L, ``nl`` = net
        liquidation). IBKR fills that endpoint lazily: the first call of a
        session answers ``{"upnl": {}}`` and later calls carry the figures, so
        we read it twice. None (poller skips) when IBKR has no day figure."""
        try:
            row: dict[str, Any] = {}
            for attempt in range(2):
                body = self._request("GET", "/iserver/account/pnl/partitioned")
                upnl = body.get("upnl") if isinstance(body, dict) else None
                if isinstance(upnl, dict) and upnl:
                    # Keys look like "DUN603294.Core"; take ours (or the only one).
                    row = next(
                        (v for k, v in upnl.items() if str(k).upper().startswith(self._account_id)),
                        next(iter(upnl.values())),
                    ) or {}
                    if row.get("dpl") is not None:
                        break
                if attempt == 0:
                    time.sleep(0.75)
            todays_pl = _to_dec(row.get("dpl"))
            # The partitioned feed's ``nl`` is a coarse figure (live paper:
            # 1030000.0 against a summary net liquidation of 1031939.06), so
            # take equity from the account summary and keep ``nl`` as a fallback.
            equity = self._summary_amount(
                self._portfolio_request("GET", f"/portfolio/{self._account_id}/summary"),
                "netliquidation",
            )
            if equity is None:
                equity = _to_dec(row.get("nl"))
            if equity is None or todays_pl is None:
                return None
            return {
                "todays_pl": todays_pl,
                "equity": equity,
                "beginning_day_balance": equity - todays_pl,
            }
        except Exception:  # noqa: BLE001
            log.warning("ibkr get_pnl_snapshot failed", exc_info=True)
            return None

    # ── Quotes ────────────────────────────────────────────────────────────

    def get_stock_latest_price(self, symbol: str) -> Decimal | None:
        """Last traded price for a stock, or None. The copy engine uses it to
        price a marketable LIMIT pre/post-market — without it a mirror goes out
        as a MARKET order, which IBKR cancels on arrival outside regular hours
        (paper, 2026-10-07: every pre-market mirror came back Cancelled).

        The shared Alpaca data feed is asked first (same price, no IBKR
        session traffic); IBKR's snapshot is the fallback. The snapshot
        endpoint needs one priming call before it returns fields."""
        try:
            from app.services import market_data_stream as mds  # noqa: PLC0415
            px = mds.data_stock_price(symbol)
            if px is not None and px > 0:
                return px
        except Exception:  # noqa: BLE001
            pass
        try:
            conid = self._conid_for(symbol)
            params = {"conids": conid, "fields": "31,84,86"}
            rows = self._request("GET", "/iserver/marketdata/snapshot", params=params)
            row = rows[0] if isinstance(rows, list) and rows else {}
            if not _attr(row, "31"):
                time.sleep(0.5)
                rows = self._request("GET", "/iserver/marketdata/snapshot", params=params)
                row = rows[0] if isinstance(rows, list) and rows else {}
            raw = str(_attr(row, "31") or "").strip()
            # IBKR prefixes the last price with a marker outside RTH ("C" for a
            # close, "H" halted); strip anything that isn't part of the number.
            raw = re.sub(r"[^0-9.]", "", raw)
            px = Decimal(raw) if raw else None
            if px is None or px <= 0:
                for f in ("84", "86"):          # bid / ask as a last resort
                    alt = _to_dec(re.sub(r"[^0-9.]", "", str(_attr(row, f) or "")))
                    if alt and alt > 0:
                        return alt
                return None
            return px
        except Exception as exc:  # noqa: BLE001
            log.info("ibkr: latest price for %s unavailable: %s", symbol, exc)
            return None

    # ── Positions ─────────────────────────────────────────────────────────

    def get_positions(self, *, cached_ok: bool = False) -> list[BrokerPosition]:
        out: list[BrokerPosition] = []
        page = 0
        while True:
            body = self._portfolio_request(
                "GET", f"/portfolio/{self._account_id}/positions/{page}"
            )
            rows = body if isinstance(body, list) else (
                body.get("positions") if isinstance(body, dict) else []
            )
            if not rows:
                break
            for p in rows:
                qty = _to_dec(_attr(p, "position", "quantity")) or Decimal(0)
                if qty == 0:
                    continue
                desc = str(_attr(p, "contractDesc", "ticker", "symbol") or "")
                sec_type = (_attr(p, "secType", "assetClass") or "").upper()
                conid = _attr(p, "conid", "conId")
                if sec_type in ("OPT", "FOP"):
                    details = self.option_details(conid) if conid is not None else None
                    if details is None:
                        details = parse_contract_desc(desc)
                    if details is not None:
                        root, expiry, strike, right = details
                        out.append(BrokerPosition(
                            broker_symbol=build_occ_symbol(root, expiry, strike, right),
                            symbol=root,
                            instrument_type=InstrumentType.OPTION,
                            quantity=qty,
                            avg_entry_price=_to_dec(_attr(p, "avgPrice", "avgCost")),
                            current_price=_to_dec(_attr(p, "mktPrice", "marketPrice")),
                            market_value=_to_dec(_attr(p, "mktValue", "marketValue")),
                            unrealized_pnl=_to_dec(_attr(p, "unrealizedPnl")),
                            cost_basis=None,
                            option_expiry=expiry,
                            option_strike=strike,
                            option_right=right,
                        ))
                        continue
                    log.warning("ibkr: option position %s (%r) unparsed; kept without legs", conid, desc)
                out.append(BrokerPosition(
                    broker_symbol=str(conid or desc),
                    symbol=desc.split(" ")[0].upper(),
                    instrument_type=(
                        InstrumentType.OPTION if sec_type in ("OPT", "FOP") else InstrumentType.STOCK
                    ),
                    quantity=qty,
                    avg_entry_price=_to_dec(_attr(p, "avgCost", "avg_cost", "avgPrice")),
                    current_price=_to_dec(_attr(p, "mktPrice", "marketPrice")),
                    market_value=_to_dec(_attr(p, "mktValue", "marketValue")),
                    unrealized_pnl=_to_dec(_attr(p, "unrealizedPnl")),
                    cost_basis=None,
                ))
            page += 1
            # IBKR pages positions 100 at a time; an under-full page is the
            # last one. Cap defensively.
            if len(rows) < 100 or page > 20:
                break
        return out

    # ── Recent orders (for the listener poll) ─────────────────────────────

    def list_recent_activities(self) -> list[Any]:
        """Polled by ibkr_listener._poll_once. Returns raw IBKR order rows;
        the listener handles dedup, persistence, and fanout."""
        body = self._request("GET", "/iserver/account/orders")
        if isinstance(body, dict):
            orders = body.get("orders") or []
        elif isinstance(body, list):
            orders = body
        else:
            orders = []
        # IBKR can return orders for sibling sub-accounts; scope to ours.
        return [
            o for o in orders
            if not _attr(o, "acctId", "account")
            or str(_attr(o, "acctId", "account")).upper() == self._account_id
        ]

    # ── helpers ───────────────────────────────────────────────────────────

    def _order_to_result(self, o: Any) -> BrokerOrderResult:
        broker_order_id = str(_attr(o, "orderId", "order_id", "id") or "")
        status_str = _norm_status(_attr(o, "status", "orderStatus"))
        filled = _to_dec(_attr(o, "filledQuantity", "cumQty")) or Decimal(0)
        avg = _to_dec(_attr(o, "avgPrice", "lastPrice"))
        ts = _attr(o, "lastExecutionTime_r", "lastExecutionTime", "submittedTime", "time")
        submitted_at = self._parse_ts(ts) or datetime.now(timezone.utc)
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status=_STATUS_IN.get(status_str, OrderStatus.SUBMITTED),
            submitted_at=submitted_at,
            filled_quantity=filled,
            filled_avg_price=avg,
        )

    @staticmethod
    def _parse_ts(v: Any) -> datetime | None:
        if v is None or v == "":
            return None
        if isinstance(v, (int, float)):
            # Treat anything beyond ~year 5000 in seconds as milliseconds.
            sec = v / 1000.0 if v > 10**12 else float(v)
            try:
                return datetime.fromtimestamp(sec, tz=timezone.utc)
            except (OSError, ValueError, OverflowError):
                return None
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
