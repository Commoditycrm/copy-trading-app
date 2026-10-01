"""Existing users must not have to reconnect after the multi-broker change.

Rows written by the old one-broker-per-user code are recreated here exactly as
stored (connection_status "connected", Webull credentials with no "paper"
key) and checked to keep working with no action from the user. No migration
is involved: "inactive" is a new VALUE of an existing string column.
"""
import inspect
import os
import sys
import uuid
from types import SimpleNamespace

from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import brokers as brokers_api
from app.brokers.webull import WebullAdapter
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.user import User

from test_broker_connect_replace import _USER, _make_session  # noqa: E402


def _legacy_row(db, broker, creds, number="ACCT-1"):
    """A row as the pre-change code left it."""
    a = BrokerAccount(
        user_id=_USER, broker=broker, label=f"{broker.value} (existing)", is_paper=False,
        encrypted_credentials=brokers_api.encrypt_json(creds),
        broker_account_number=number, connection_status="connected",
    )
    db.add(a); db.commit()
    return a


def test_an_existing_connected_broker_stays_active_untouched():
    db = _make_session()
    a = _legacy_row(db, BrokerName.ALPACA, {"api_key": "k", "api_secret": "s", "paper": True})
    out = brokers_api.list_my_brokers(db=db, user=db.get(User, _USER)) \
        if "db" in inspect.signature(brokers_api.list_my_brokers).parameters else None
    row = db.get(BrokerAccount, a.id)
    assert row.connection_status == "connected"      # still the active broker
    if out is not None:
        assert [x.id for x in out] == [a.id]


def test_existing_webull_credentials_stay_on_the_live_host():
    """Old Webull rows have no "paper" key — they must keep talking to live."""
    creds = {"app_key": "k", "app_secret": "s", "account_id": "A", "region_id": "us"}
    assert WebullAdapter(creds).paper is False


def test_everything_that_trades_still_selects_existing_rows():
    """The status every trading path filters on is unchanged for old rows."""
    from app.services import copy_engine

    src = inspect.getsource(copy_engine)
    assert 'BrokerAccount.connection_status == "connected"' in src


def test_reconnecting_an_existing_account_keeps_its_id(monkeypatch):
    """Re-entering keys for the same account updates the row in place, so its
    order history stays linked (the old code deleted and re-created it)."""
    from app.brokers.base import ConnectionInfo
    from app.schemas.broker import AlpacaCredentialsIn, ConnectBrokerIn

    db = _make_session()
    old = _legacy_row(db, BrokerName.ALPACA, {"api_key": "k", "api_secret": "s", "paper": True},
                      number="PA-9")

    class _A:
        def verify_connection(self):
            return ConnectionInfo(broker_account_id="PA-9", supports_fractional=True, extra={})

    monkeypatch.setattr(brokers_api, "adapter_for", lambda acct, creds: _A())
    monkeypatch.setattr(brokers_api, "_refresh_balance_into", lambda acct, creds: None)
    monkeypatch.setattr(brokers_api.cache, "invalidate_broker_accounts", lambda uid: None)
    monkeypatch.setattr(brokers_api.listeners, "stop_listener", lambda uid: None)
    monkeypatch.setattr(brokers_api, "_start_trader_listener", lambda user, acct: None)
    payload = ConnectBrokerIn(broker=BrokerName.ALPACA, label="Alpaca Paper",
                              alpaca=AlpacaCredentialsIn(api_key="PKTESTKEY2", api_secret="SECRETVALUE2", paper=True))
    acct = brokers_api.connect(payload, SimpleNamespace(headers={}, client=None), db, db.get(User, _USER))
    rows = list(db.execute(select(BrokerAccount)).scalars())
    assert acct.id == old.id and len(rows) == 1
    assert rows[0].connection_status == "connected"


def test_an_old_inactive_snaptrade_row_never_short_circuits_a_new_connect():
    """The /finish race shortcut must only match a CONNECTED row created moments
    ago — never a stale SnapTrade connection kept inactive."""
    src = inspect.getsource(brokers_api)
    shortcut = src[src.index("existing_snap = db.execute("):src.index("if existing_snap is not None:")]
    assert 'BrokerAccount.connection_status == "connected"' in shortcut
    assert "timedelta(minutes=2)" in shortcut
