"""IBKR adapter — OAuth 1.0a handshake, request signing, contract resolution.

No network: a fake IBKR sits behind ``requests.post`` / ``requests.request``
and does the server half of the protocol independently — it verifies the
RSA-SHA256 signature on the live-session-token request against the user's
public key, derives the shared secret from its own DH exponent, and checks
the HMAC-SHA256 signature on every later call. If our signing drifted from
the spec, the fake would reject it the same way IBKR would.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json as jsonlib
import secrets
import urllib.parse
from datetime import date
from decimal import Decimal

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.brokers import ibkr as ibkr_mod
from app.brokers.base import BrokerOrderRequest
from app.brokers.ibkr import IBKRAdapter, build_occ_symbol, parse_contract_desc
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderType

# RFC 3526 group 5 (1536-bit MODP). Any large odd modulus exercises the DH
# arithmetic; this one is simply well known.
_P_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA237327FFFFFFFFFFFFFFFF"
)
CONSUMER_KEY = "TESTCONS1"
ACCESS_TOKEN = "atok-123"
ACCOUNT_ID = "U7654321"


def _pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = jsonlib.dumps(body) if body is not None else ""

    def json(self):
        return self._body


def _parse_auth_header(header: str) -> dict[str, str]:
    assert header.startswith("OAuth ")
    out: dict[str, str] = {}
    for part in header[len("OAuth "):].split(", "):
        k, v = part.split("=", 1)
        out[k] = v.strip('"')
    return out


class FakeIBKR:
    """Server half of IBKR's OAuth 1.0a + a few endpoints."""

    def __init__(self, p: int, sig_pub, token_secret_plain: bytes):
        self.p = p
        self.sig_pub = sig_pub
        self.secret = token_secret_plain
        self.lst: bytes | None = None
        self.handshakes = 0
        self.calls: list[tuple[str, str, dict | None, object]] = []
        self.brokerage_authenticated = False
        self.placed: list[dict] = []
        self.feed: list[dict] = []
        self.cancel_503_once = False
        self.snapshot_calls = 0
        self.pnl_calls = 0
        self.search_results = [
            {"conid": 265598, "symbol": "AAPL", "secType": "STK", "description": "NASDAQ"},
        ]
        self.info_results = [
            {"conid": 700000001, "maturityDate": "20261016", "strike": 200.0, "right": "C"},
            {"conid": 700000002, "maturityDate": "20261023", "strike": 200.0, "right": "C"},
        ]

    # ── oauth/live_session_token ─────────────────────────────────────────
    def post(self, url, headers=None, timeout=None):
        assert url == ibkr_mod.BASE_URL + ibkr_mod.LST_PATH
        params = _parse_auth_header(headers["Authorization"])
        assert params.pop("realm") == "limited_poa"
        assert params["oauth_signature_method"] == "RSA-SHA256"
        signature = base64.b64decode(urllib.parse.unquote(params.pop("oauth_signature")))
        # Rebuild the base string the way the spec (and IBKR) does.
        param_str = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
            for k, v in sorted(params.items())
        )
        base = (
            self.secret.hex()
            + "POST&" + urllib.parse.quote(url, safe="")
            + "&" + urllib.parse.quote(param_str, safe="")
        )
        try:
            self.sig_pub.verify(signature, base.encode(), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            return _Resp(401, {"error": "invalid signature"})

        a_pub = int(params["diffie_hellman_challenge"], 16)
        b = secrets.randbits(256)
        B = pow(2, b, self.p)
        K = pow(a_pub, b, self.p)
        k_hex = format(K, "x")
        if len(k_hex) % 2:
            k_hex = "0" + k_hex
        k_bytes = bytes.fromhex(k_hex)
        if k_bytes[0] & 0x80:
            k_bytes = b"\x00" + k_bytes
        self.lst = hmac.new(k_bytes, self.secret, hashlib.sha1).digest()
        self.handshakes += 1
        return _Resp(200, {
            "diffie_hellman_response": format(B, "x"),
            "live_session_token_signature": hmac.new(
                self.lst, CONSUMER_KEY.encode(), hashlib.sha1
            ).hexdigest(),
            "live_session_token_expiration": 4_102_444_800_000,
        })

    # ── signed API calls ─────────────────────────────────────────────────
    def request(self, method, url, params=None, json=None, headers=None, timeout=None, verify=True):
        assert verify is True, "hosted IBKR must keep TLS verification"
        oauth = _parse_auth_header(headers["Authorization"])
        oauth.pop("realm")
        assert oauth["oauth_signature_method"] == "HMAC-SHA256"
        signature = base64.b64decode(urllib.parse.unquote(oauth.pop("oauth_signature")))
        all_params = {**(params or {}), **oauth}
        param_str = "&".join(
            f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
            for k, v in sorted(all_params.items())
        )
        base = f"{method}&{urllib.parse.quote(url, safe='')}&{urllib.parse.quote(param_str, safe='')}"
        assert self.lst is not None, "signed call before any handshake"
        expected = hmac.new(self.lst, base.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, signature):
            return _Resp(401, {"error": "bad signature"})

        path = url[len(ibkr_mod.BASE_URL):]
        self.calls.append((method, path, params, json))
        if path == "/iserver/auth/status":
            return _Resp(200, {"authenticated": self.brokerage_authenticated})
        if path == "/iserver/auth/ssodh/init":
            self.brokerage_authenticated = True
            return _Resp(200, {"authenticated": True})
        if path == "/portfolio/accounts":
            return _Resp(200, [{"accountId": ACCOUNT_ID}, {"accountId": "U0000001"}])
        if path == "/iserver/secdef/search":
            return _Resp(200, self.search_results)
        if path == "/iserver/secdef/info":
            return _Resp(200, self.info_results)
        if path == "/iserver/account/orders":
            return _Resp(200, {"orders": self.feed})
        if path.startswith(f"/iserver/account/{ACCOUNT_ID}/order/") and method == "DELETE":
            oid = path.rsplit("/", 1)[1]
            live = next((o for o in self.feed if str(o.get("orderId")) == oid and o.get("status") in ("Submitted", "PreSubmitted")), None)
            if live is not None and self.cancel_503_once:
                # The paper gateway's quirk: cancel performed, 503 returned.
                self.cancel_503_once = False
                live["status"] = "Cancelled"
                return _Resp(503, {"error": "Service Unavailable", "statusCode": 503})
            if live is not None:
                live["status"] = "Cancelled"
                return _Resp(200, [{"msg": "Request was submitted"}])
            if any(str(o.get("orderId")) == oid for o in self.feed):
                return _Resp(400, {"error": f"Cancel attempted when order is not in a cancellable state.  Order permId ={oid}"})
            return _Resp(400, {"error": f"OrderID {oid} doesn't exist"})
        if path == f"/portfolio/{ACCOUNT_ID}/summary":
            return _Resp(200, {
                "netliquidation": {"amount": 1031939.0625, "currency": "USD", "isNull": False},
                "totalcashvalue": {"amount": 1031446.875, "currency": "USD", "isNull": False},
                "buyingpower": {"amount": 4125824.25, "currency": "USD", "isNull": False},
            })
        if path == "/iserver/account/pnl/partitioned":
            self.pnl_calls += 1
            if self.pnl_calls == 1:
                return _Resp(200, {"upnl": {}})
            return _Resp(200, {"upnl": {f"{ACCOUNT_ID}.Core": {"dpl": -12.5, "nl": 1031939.06, "upl": 3.0}}})
        if path == "/iserver/marketdata/snapshot":
            self.snapshot_calls += 1
            if self.snapshot_calls == 1:
                return _Resp(200, [{"conid": 265598}])
            return _Resp(200, [{"conid": 265598, "31": "C331.85", "84": "331.80", "86": "331.90"}])
        if path == f"/iserver/account/{ACCOUNT_ID}/orders":
            self.placed.append(json["orders"][0])
            return _Resp(200, [{"id": "confirm-1", "message": ["Are you sure?"]}])
        if path == "/iserver/reply/confirm-1":
            return _Resp(200, [{"order_id": "9001", "order_status": "PreSubmitted"}])
        if path == "/iserver/contract/700000001/info":
            return _Resp(200, {
                "symbol": "AAPL", "local_symbol": "AAPL  261016C00200000",
                "maturity_date": "20261016", "strike": 200, "right": "C",
            })
        return _Resp(404, {"error": f"unrouted {path}"})


@pytest.fixture
def world(monkeypatch):
    sig_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    enc_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    secret_plain = secrets.token_bytes(32)
    encrypted_secret = base64.b64encode(
        enc_key.public_key().encrypt(secret_plain, padding.PKCS1v15())
    ).decode()
    creds = {
        "consumer_key": CONSUMER_KEY,
        "access_token": ACCESS_TOKEN,
        "access_token_secret": encrypted_secret,
        "private_signature_key": _pem(sig_key),
        "private_encryption_key": _pem(enc_key),
        "dh_prime": _P_HEX,
        "account_id": ACCOUNT_ID,
    }
    fake = FakeIBKR(int(_P_HEX, 16), sig_key.public_key(), secret_plain)
    monkeypatch.setattr(ibkr_mod.requests, "post", fake.post)
    monkeypatch.setattr(ibkr_mod.requests, "request", fake.request)
    monkeypatch.setattr(ibkr_mod.time, "sleep", lambda *_: None)
    # Fresh process-wide caches per test.
    monkeypatch.setattr(ibkr_mod, "_SESSIONS", {})
    monkeypatch.setattr(IBKRAdapter, "_conid_cache", {})
    monkeypatch.setattr(IBKRAdapter, "_option_detail_cache", {})
    return creds, fake


def test_verify_connection_handshakes_and_opens_brokerage_session(world):
    creds, fake = world
    info = IBKRAdapter(creds).verify_connection()
    assert fake.handshakes == 1
    assert info.broker_account_id == ACCOUNT_ID
    assert info.extra["accounts"] == ["U0000001", ACCOUNT_ID]
    paths = [p for _, p, _, _ in fake.calls]
    assert paths[:3] == ["/iserver/auth/status", "/iserver/auth/ssodh/init", "/portfolio/accounts"]


def test_wrong_account_id_is_reported(world):
    creds, _ = world
    with pytest.raises(RuntimeError, match="isn't in the connected accounts"):
        IBKRAdapter({**creds, "account_id": "U9999999"}).verify_connection()


def test_session_is_shared_across_adapter_instances(world):
    creds, fake = world
    IBKRAdapter(creds).verify_connection()
    IBKRAdapter(creds).list_recent_activities()
    assert fake.handshakes == 1, "a second adapter for the same token must reuse the LST"


def test_401_triggers_one_rehandshake(world):
    creds, fake = world
    adapter = IBKRAdapter(creds)
    adapter.verify_connection()
    fake.lst = None  # IBKR forgot our token
    fake.brokerage_authenticated = False
    real_request = fake.request

    def flaky(method, url, **kw):
        if fake.lst is None:
            return _Resp(401, {"error": "Unauthorized"})
        return real_request(method, url, **kw)

    ibkr_mod.requests.request = flaky
    assert adapter.list_recent_activities() == [] or True  # must not raise
    assert fake.handshakes == 2


def test_wrong_encryption_key_is_rejected_at_handshake(world):
    # OpenSSL 3 decrypts PKCS#1 v1.5 with implicit rejection (random bytes
    # instead of an error), so a wrong encryption key can only surface as
    # IBKR refusing the signed handshake — which must read as a credentials
    # problem, not a network one.
    creds, fake = world
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # Depending on the padding bytes OpenSSL either raises (→ "could not be
    # decrypted") or hands back random bytes (→ IBKR rejects the signed
    # handshake). Both are credential errors; both must stay RuntimeErrors.
    with pytest.raises(RuntimeError, match="rejected the live-session-token request|could not be decrypted"):
        IBKRAdapter({**creds, "private_encryption_key": _pem(other)}).verify_connection()
    assert fake.handshakes == 0


def test_old_single_signing_key_shape_is_rejected_clearly():
    with pytest.raises(RuntimeError, match="old single-signing-key"):
        IBKRAdapter({
            "consumer_key": "x", "signing_key": "y", "access_token": "z",
            "access_token_secret": "w", "account_id": "U1",
        })


def test_dh_prime_accepts_hex_and_pem():
    p = int(_P_HEX, 16)
    assert ibkr_mod._parse_dh_prime(_P_HEX) == p
    assert ibkr_mod._parse_dh_prime("0x" + _P_HEX.lower()) == p
    with pytest.raises(RuntimeError, match="hex"):
        ibkr_mod._parse_dh_prime("not-hex")


def test_option_order_resolves_conid_and_clears_confirmation(world):
    creds, fake = world
    adapter = IBKRAdapter(creds)
    res = adapter.place_order(BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION,
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("2"),
        limit_price=Decimal("3.45"),
        option_expiry=date(2026, 10, 16),
        option_strike=Decimal("200"),
        option_right=OptionRight.CALL,
        client_order_id="11111111-2222-3333-4444-555555555555",
    ))
    assert res.broker_order_id == "9001"
    assert len(fake.placed) == 1
    order = fake.placed[0]
    assert order["conid"] == 700000001           # the 16-Oct row, not 23-Oct
    assert order["secType"] == "700000001:OPT"
    assert order["orderType"] == "LMT" and order["price"] == 3.45
    assert order["quantity"] == 2 and order["side"] == "BUY"
    assert order["cOID"] == "11111111222233334444555555555555"  # 32-char hex, fits IBKR's cap
    info_call = next(c for c in fake.calls if c[1] == "/iserver/secdef/info")
    assert info_call[2] == {
        "conid": "265598", "sectype": "OPT", "month": "OCT26",
        "strike": "200", "right": "C", "exchange": "SMART",
    }
    # Resolution is cached: a second order makes no further lookups.
    n_lookups = sum(1 for c in fake.calls if c[1].startswith("/iserver/secdef"))
    adapter.place_order(BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION, symbol="AAPL", side=OrderSide.SELL,
        order_type=OrderType.MARKET, quantity=Decimal("2"),
        option_expiry=date(2026, 10, 16), option_strike=Decimal("200"),
        option_right=OptionRight.CALL,
    ))
    assert sum(1 for c in fake.calls if c[1].startswith("/iserver/secdef")) == n_lookups


def test_option_with_no_matching_expiry_is_a_clear_error(world):
    creds, _ = world
    with pytest.raises(RuntimeError, match="has no AAPL 2026-11-20 200C contract"):
        IBKRAdapter(creds).place_order(BrokerOrderRequest(
            instrument_type=InstrumentType.OPTION, symbol="AAPL", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=Decimal("1"),
            option_expiry=date(2026, 11, 20), option_strike=Decimal("200"),
            option_right=OptionRight.CALL,
        ))


def test_stock_order_body(world):
    creds, fake = world
    IBKRAdapter(creds).place_order(BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=Decimal("10.5"), limit_price=Decimal("190"),
        extended_hours=True,
    ))
    order = fake.placed[0]
    assert order["secType"] == "265598:STK" and order["quantity"] == 10.5
    assert order["outsideRTH"] is True


def test_unsupported_order_type_never_degrades_to_market(world):
    creds, fake = world
    with pytest.raises(ValueError, match="not supported"):
        IBKRAdapter(creds).place_order(BrokerOrderRequest(
            instrument_type=InstrumentType.STOCK, symbol="AAPL", side=OrderSide.SELL,
            order_type=OrderType.TRAILING_STOP, quantity=Decimal("1"), trail_percent=Decimal("5"),
        ))
    assert fake.placed == []


def test_option_details_from_contract_info(world):
    creds, _ = world
    assert IBKRAdapter(creds).option_details(700000001) == (
        "AAPL", date(2026, 10, 16), Decimal("200"), OptionRight.CALL,
    )


@pytest.mark.parametrize("desc,expected", [
    ("AAPL 06JUN26 200 C", ("AAPL", date(2026, 6, 6), Decimal("200"), OptionRight.CALL)),
    ("SPY 17OCT26 452.5 P", ("SPY", date(2026, 10, 17), Decimal("452.5"), OptionRight.PUT)),
    ("SPY DEC 19 '25 600 Call", ("SPY", date(2025, 12, 19), Decimal("600"), OptionRight.CALL)),
    ("TSLA  260116P00250000", ("TSLA", date(2026, 1, 16), Decimal("250"), OptionRight.PUT)),
    ("AAPL", None),
    ("", None),
    (None, None),
])
def test_parse_contract_desc(desc, expected):
    assert parse_contract_desc(desc) == expected


def test_build_occ_symbol():
    assert build_occ_symbol("spy", date(2026, 10, 17), Decimal("452.5"), OptionRight.PUT) == "SPY261017P00452500"


# ── Gateway mode (Client Portal Gateway on the backend's machine) ───────────


class FakeGateway:
    """A Client Portal Gateway: same endpoints, no OAuth, self-signed TLS."""

    def __init__(self):
        self.logged_in = True
        self.authenticated = True
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, verify=True):
        assert verify is False, "gateway has a self-signed certificate"
        assert "Authorization" not in headers, "gateway calls must not be OAuth-signed"
        assert url.startswith("https://localhost:5000/v1/api")
        path = url[len("https://localhost:5000/v1/api"):]
        self.calls.append((method, path))
        if not self.logged_in:
            return _Resp(401, {"error": "not authenticated"})
        if path == "/iserver/auth/status":
            return _Resp(200, {"authenticated": self.authenticated, "connected": True})
        if path == "/iserver/auth/ssodh/init":
            self.authenticated = True
            return _Resp(200, {"authenticated": True})
        if path == "/portfolio/accounts":
            return _Resp(200, [{"accountId": "DU1234567"}])
        if path == "/iserver/account/orders":
            return _Resp(200, {"orders": []})
        return _Resp(404, {"error": f"unrouted {path}"})


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr(ibkr_mod.requests, "request", fake.request)
    monkeypatch.setattr(ibkr_mod.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no OAuth handshake in gateway mode")))
    monkeypatch.setattr(ibkr_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ibkr_mod, "_SESSIONS", {})
    monkeypatch.setattr(ibkr_mod, "_start_gateway_keepalive", lambda *_: None)
    return fake


GATEWAY_CREDS = {"mode": "gateway", "gateway_url": "https://localhost:5000", "account_id": "DU1234567", "paper": True}


def test_gateway_verify_connection(gateway):
    info = IBKRAdapter(GATEWAY_CREDS).verify_connection()
    assert info.broker_account_id == "DU1234567"
    assert info.extra["mode"] == "gateway" and info.extra["paper"] is True
    assert ("GET", "/portfolio/accounts") in gateway.calls


def test_gateway_reinitialises_timed_out_brokerage_session(gateway):
    gateway.authenticated = False  # idle timeout: connected but not authenticated
    IBKRAdapter(GATEWAY_CREDS).list_recent_activities()
    assert ("POST", "/iserver/auth/ssodh/init") in gateway.calls


def test_gateway_not_logged_in_is_a_clear_user_message(gateway):
    gateway.logged_in = False
    with pytest.raises(ibkr_mod.IBKRAuthError, match="Open https://localhost:5000 in a browser"):
        IBKRAdapter(GATEWAY_CREDS).verify_connection()
    # No OAuth re-handshake attempted (the fixture's requests.post would assert).


def test_gateway_url_defaults_and_is_inferred_from_gateway_url(gateway):
    a = IBKRAdapter({"gateway_url": "localhost:5000", "account_id": "du1234567"})
    assert a._gateway and a._base_url == "https://localhost:5000/v1/api" and a._account_id == "DU1234567"


@pytest.mark.parametrize("url", [
    "https://localhost:5000", "http://127.0.0.1:5000", "https://192.168.1.20:5000",
    "https://100.101.102.103:5000", "https://my-mac.ts.net:5000", "https://gw.lan",
])
def test_gateway_url_private_hosts_allowed(url):
    assert ibkr_mod.normalize_gateway_url(url).startswith(url.split("://")[0])


@pytest.mark.parametrize("url", [
    "https://api.ibkr.com", "https://8.8.8.8:5000", "https://example.com:5000",
    "https://localhost:5000/v1/api", "ftp://localhost:5000",
])
def test_gateway_url_public_or_malformed_rejected(url):
    with pytest.raises(RuntimeError):
        ibkr_mod.normalize_gateway_url(url)



# ── Listener helpers (pure functions, no DB) ───────────────────────────────


def test_listener_reads_order_ref_as_app_order_id():
    from app.services import ibkr_listener as L
    import uuid as _uuid
    oid = _uuid.UUID("9db66cb3-73f1-47f8-85b0-a55d9e2f1234")
    assert L._app_order_ref_uuid({"order_ref": oid.hex}) == oid          # what the adapter now sends
    assert L._app_order_ref_uuid({"cOID": str(oid)}) == oid               # older payload shape
    assert L._app_order_ref_uuid({"order_ref": "9db66cb3-73f1-47f8-85b0-a55d9e2f"}) is None  # truncated dashed → unusable
    assert L._app_order_ref_uuid({"order_ref": "37065808"}) is None       # IBKR's own ref on external orders
    assert L._app_order_ref_uuid({}) is None


@pytest.mark.parametrize("row,expected", [
    ({"orderType": "Limit", "price": "320.00"}, OrderType.LIMIT),
    ({"orderType": "Market"}, OrderType.MARKET),
    ({"orderType": "Stop Limit"}, OrderType.STOP_LIMIT),
    ({"orderType": "LMT"}, OrderType.LIMIT),
    ({"orderType": "Weird", "price": "10"}, OrderType.LIMIT),   # unmapped + priced → never MARKET
    ({"orderType": "Weird"}, OrderType.MARKET),
])
def test_listener_maps_feed_order_types(row, expected):
    from app.services import ibkr_listener as L
    assert L._order_type_in(row) == expected



# ── Cancel semantics + quotes ───────────────────────────────────────────────


def test_cancel_of_already_cancelled_order_returns_false(world):
    creds, fake = world
    fake.feed = [{"orderId": 777, "ticker": "NIO", "side": "BUY", "status": "Cancelled", "filledQuantity": 0.0}]
    assert IBKRAdapter(creds).cancel_order("777") is False


def test_cancel_of_live_order_returns_true(world):
    creds, fake = world
    fake.feed = [{"orderId": 778, "ticker": "NIO", "side": "BUY", "status": "Submitted", "filledQuantity": 0.0}]
    assert IBKRAdapter(creds).cancel_order("778") is True


def test_cancel_of_unknown_order_still_raises(world):
    creds, _ = world
    with pytest.raises(RuntimeError, match="doesn't exist"):
        IBKRAdapter(creds).cancel_order("999")


def test_latest_price_falls_back_to_ibkr_snapshot(world, monkeypatch):
    creds, fake = world
    from app.services import market_data_stream as mds
    monkeypatch.setattr(mds, "data_stock_price", lambda s: None)
    px = IBKRAdapter(creds).get_stock_latest_price("AAPL")
    assert px == Decimal("331.85")           # "C" close-marker stripped
    assert fake.snapshot_calls == 2          # primed, then read


def test_cancel_acknowledged_with_503_is_settled_by_reading_back(world):
    creds, fake = world
    fake.feed = [{"orderId": 779, "ticker": "NIO", "side": "BUY", "status": "Submitted", "filledQuantity": 0.0}]
    fake.cancel_503_once = True
    assert IBKRAdapter(creds).cancel_order("779") is True
    assert fake.feed[0]["status"] == "Cancelled"


def test_cancel_of_filled_order_returns_false_on_not_cancellable(world):
    creds, fake = world
    fake.feed = [{"orderId": 780, "ticker": "NIO", "side": "BUY", "status": "Filled", "filledQuantity": 1.0, "avgPrice": "3.4"}]
    assert IBKRAdapter(creds).cancel_order("780") is False


def test_balance_snapshot_reads_nested_summary(world):
    creds, _ = world
    b = IBKRAdapter(creds).get_balance_snapshot()
    assert b == {
        "cash": Decimal("1031446.875"), "buying_power": Decimal("4125824.25"),
        "total_equity": Decimal("1031939.0625"), "currency": "USD",
    }


def test_pnl_snapshot_primes_then_reads(world):
    creds, fake = world
    p = IBKRAdapter(creds).get_pnl_snapshot()
    assert p == {
        "todays_pl": Decimal("-12.5"), "equity": Decimal("1031939.0625"),   # equity from the summary, not the coarse nl
        "beginning_day_balance": Decimal("1031951.5625"),
    }
    assert fake.pnl_calls == 2
