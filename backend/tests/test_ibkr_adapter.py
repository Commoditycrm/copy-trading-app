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
    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
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
            return _Resp(200, {"orders": []})
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
    with pytest.raises(ibkr_mod.IBKRAuthError, match="rejected the live-session-token request"):
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
    assert order["cOID"] == "11111111-2222-3333-4444-555555555555"[:32]
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
