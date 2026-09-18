"""POST /api/brokers/webull/accounts — the connect-time account picker.

A Webull app_key reaches EVERY account under that login (Cash, Margin, IRA,
Futures), and which one we trade is decided purely by the `account_id` stored in
the credentials. That id appears nowhere in the Webull app, so the previous
free-text field asked users to guess — and a real-but-wrong id passes
`verify_connection` cleanly, after which every mirror order trades in the wrong
account with nothing to flag it.

This endpoint exchanges the keys for the list of tradable accounts (with
balances, since equity is what distinguishes a funded account from an empty one)
so the UI can offer a picker. It is a READ: the keys are used for the call and
discarded, and nothing is persisted unless the user goes on to connect.

Offline — the adapter is stubbed; no SDK, no network.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decimal import Decimal

from fastapi import HTTPException

from app.api import brokers as brokers_api
from app.models.user import User, UserRole
from app.schemas.broker import ListWebullAccountsIn, WebullAccountOut

_USER = uuid.UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")


class _User:
    """current_user is only read for .id here."""
    id = _USER
    role = UserRole.SUBSCRIBER


def _payload():
    return ListWebullAccountsIn(
        app_key="appkey12345", app_secret="appsecret12345", region_id="us",
    )


class _StubAdapter:
    """Replaces WebullAdapter for the endpoint's one call."""
    instances: list = []

    def __init__(self, creds):
        self.creds = creds
        _StubAdapter.instances.append(self)

    result: object = []

    def list_accounts(self, with_balances=False):
        self.with_balances = with_balances
        if isinstance(_StubAdapter.result, Exception):
            raise _StubAdapter.result
        return _StubAdapter.result


class _Patched:
    def __init__(self, result, enabled=True):
        self._result, self._enabled = result, enabled

    def __enter__(self):
        _StubAdapter.instances = []
        _StubAdapter.result = self._result
        self._saved_adapter = brokers_api.WebullAdapter
        self._saved_settings = brokers_api.get_settings
        brokers_api.WebullAdapter = _StubAdapter

        enabled = self._enabled

        class _S:
            webull_direct_enabled = enabled

        brokers_api.get_settings = lambda: _S()
        return self

    def __exit__(self, *exc):
        brokers_api.WebullAdapter = self._saved_adapter
        brokers_api.get_settings = self._saved_settings


_ACCOUNTS = [
    {"account_id": "ACC-CASH", "account_number": "8XX111", "account_type": "CASH",
     "currency": "USD", "total_equity": Decimal("5200.75"),
     "buying_power": Decimal("10400.00")},
    {"account_id": "ACC-FUT", "account_number": "8XX222", "account_type": "FUTURES",
     "currency": "USD", "total_equity": Decimal("0"), "buying_power": Decimal("0")},
]


def test_returns_every_tradable_account():
    with _Patched(_ACCOUNTS):
        out = brokers_api.list_webull_accounts(_payload(), _User())
    assert [a["account_id"] for a in out] == ["ACC-CASH", "ACC-FUT"]


def test_response_model_carries_the_balance():
    """The picker's whole job is letting someone tell the funded account from the
    empty one, so equity has to survive serialisation."""
    with _Patched(_ACCOUNTS):
        out = brokers_api.list_webull_accounts(_payload(), _User())
    rendered = [WebullAccountOut.model_validate(a) for a in out]
    assert rendered[0].total_equity == Decimal("5200.75")
    assert rendered[1].total_equity == Decimal("0")
    assert rendered[0].account_number == "8XX111"


def test_balances_are_requested():
    with _Patched(_ACCOUNTS):
        brokers_api.list_webull_accounts(_payload(), _User())
    assert _StubAdapter.instances[0].with_balances is True


def test_keys_are_passed_through_trimmed_and_not_persisted():
    """A read-only exchange: the credentials are used for this call and dropped.
    Nothing here writes a BrokerAccount — that only happens on connect."""
    payload = ListWebullAccountsIn(
        app_key="  appkey12345  ", app_secret=" appsecret12345 ", region_id=" us ",
    )
    with _Patched(_ACCOUNTS):
        brokers_api.list_webull_accounts(payload, _User())
    creds = _StubAdapter.instances[0].creds
    assert creds["app_key"] == "appkey12345"
    assert creds["app_secret"] == "appsecret12345"
    assert creds["region_id"] == "us"
    # No account_id yet — that is precisely what the user is about to choose.
    assert "account_id" not in creds


def test_blank_region_defaults_to_us():
    payload = ListWebullAccountsIn(
        app_key="appkey12345", app_secret="appsecret12345", region_id="",
    )
    with _Patched(_ACCOUNTS):
        brokers_api.list_webull_accounts(payload, _User())
    assert _StubAdapter.instances[0].creds["region_id"] == "us"


def test_refused_when_direct_webull_is_disabled():
    """Same gate as connect — the feature is off server-side, say so clearly
    rather than making a broker call."""
    with _Patched(_ACCOUNTS, enabled=False):
        try:
            brokers_api.list_webull_accounts(_payload(), _User())
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "not enabled" in str(exc.detail)
            assert _StubAdapter.instances == []   # never reached the broker
            return
    raise AssertionError("expected a 400 when webull_direct_enabled is off")


def test_bad_keys_surface_as_an_actionable_400():
    """Raw SDK text tells the user nothing. Rejected credentials should name the
    two things they can actually check."""
    with _Patched(RuntimeError("HTTP Status: 401, Code: UNAUTHORIZED, Msg: ")):
        try:
            brokers_api.list_webull_accounts(_payload(), _User())
        except HTTPException as exc:
            assert exc.status_code == 400
            detail = str(exc.detail)
            assert "app key and secret" in detail and "Trading API" in detail
            return
    raise AssertionError("expected a 400 for rejected credentials")


def test_unapproved_token_tells_the_user_to_authorise_in_the_app():
    """THE first-connect case. Webull issues the token in PENDING status and it
    stays unusable until the owner authorises it in their Webull app; until then
    every call fails with a raw 'ERROR_INIT_TOKEN ... status:PENDING' that says
    nothing about what to do. This is the normal path, not an edge case."""
    with _Patched(RuntimeError(
        "ERROR_INIT_TOKEN init_token status not verified error. "
        "token:ab**cd expires:1 status:PENDING"
    )):
        try:
            brokers_api.list_webull_accounts(_payload(), _User())
        except HTTPException as exc:
            detail = str(exc.detail)
            assert "Webull app" in detail and "authoris" in detail
            assert "ERROR_INIT_TOKEN" not in detail   # not the raw SDK dump
            return
    raise AssertionError("expected a 400 for an unapproved token")


def test_an_unrecognised_error_still_passes_the_broker_text_through():
    """Only known shapes are translated — anything else must stay debuggable."""
    with _Patched(RuntimeError("some unmapped webull failure")):
        try:
            brokers_api.list_webull_accounts(_payload(), _User())
        except HTTPException as exc:
            assert "some unmapped webull failure" in str(exc.detail)
            return
    raise AssertionError("expected a 400")


def test_authenticated_but_no_accounts_is_an_explicit_error():
    """An empty picker would look like a UI bug. Name the likely cause instead."""
    with _Patched([]):
        try:
            brokers_api.list_webull_accounts(_payload(), _User())
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "Trading API" in str(exc.detail)
            return
    raise AssertionError("expected a 400 when no accounts come back")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll webull account-picker tests passed.")
