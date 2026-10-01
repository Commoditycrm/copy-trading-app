"""Several brokers on file, exactly one ACTIVE.

Deactivate pauses a broker without deleting it; Activate switches back to a
stored one (after re-verifying its keys) and pauses whichever was active.
"""
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import brokers as brokers_api
from app.brokers.base import ConnectionInfo
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.user import User

from test_broker_connect_replace import _USER, _make_session  # noqa: E402


def _acct(db, status, label, number):
    a = BrokerAccount(
        user_id=_USER, broker=BrokerName.ALPACA, label=label, is_paper=True,
        encrypted_credentials=brokers_api.encrypt_json({"api_key": "k", "api_secret": "s", "paper": True}),
        broker_account_number=number, connection_status=status,
    )
    db.add(a); db.commit()
    return a


@pytest.fixture
def env(monkeypatch):
    calls = SimpleNamespace(stopped=0, started=[], verify_ok=True)

    class _Adapter:
        def verify_connection(self):
            if not calls.verify_ok:
                raise RuntimeError("keys revoked")
            return ConnectionInfo(broker_account_id="PA-2", supports_fractional=True, extra={})

    monkeypatch.setattr(brokers_api, "adapter_for", lambda acct, creds: _Adapter())
    monkeypatch.setattr(brokers_api, "_refresh_balance_into", lambda acct, creds: None)
    monkeypatch.setattr(brokers_api.cache, "invalidate_broker_accounts", lambda uid: None)
    monkeypatch.setattr(brokers_api.listeners, "stop_listener",
                        lambda uid: setattr(calls, "stopped", calls.stopped + 1))
    monkeypatch.setattr(brokers_api, "_start_trader_listener",
                        lambda user, acct: calls.started.append(acct.id))
    return calls


def _req():
    return SimpleNamespace(headers={}, client=None)


def _status(db):
    return {a.label: a.connection_status for a in db.execute(select(BrokerAccount)).scalars()}


def test_deactivate_pauses_without_deleting(env):
    from app.models.user import UserRole

    db = _make_session()
    db.get(User, _USER).role = UserRole.TRADER    # only traders run a listener
    a = _acct(db, "connected", "Alpaca Paper", "PA-1")
    out = brokers_api.deactivate_broker(a.id, _req(), db, db.get(User, _USER))
    assert out.connection_status == "inactive"
    assert _status(db) == {"Alpaca Paper": "inactive"}
    assert env.stopped == 1


def test_activate_switches_the_active_broker(env):
    db = _make_session()
    _acct(db, "connected", "Webull", "WB-1")
    alpaca = _acct(db, "inactive", "Alpaca Paper", "PA-2")
    brokers_api.activate_broker(alpaca.id, _req(), db, db.get(User, _USER))
    assert _status(db) == {"Webull": "inactive", "Alpaca Paper": "connected"}
    assert env.started == [alpaca.id]


def test_a_failed_verify_changes_nothing(env):
    """Stored keys can lapse while inactive; the current broker must stay live."""
    db = _make_session()
    _acct(db, "connected", "Webull", "WB-1")
    alpaca = _acct(db, "inactive", "Alpaca Paper", "PA-2")
    env.verify_ok = False
    with pytest.raises(HTTPException) as exc:
        brokers_api.activate_broker(alpaca.id, _req(), db, db.get(User, _USER))
    assert exc.value.status_code == 400
    assert _status(db) == {"Webull": "connected", "Alpaca Paper": "inactive"}
    assert env.started == [] and env.stopped == 0


def test_deleting_an_inactive_broker_leaves_the_active_listener(env):
    db = _make_session()
    _acct(db, "connected", "Webull", "WB-1")
    old = _acct(db, "inactive", "Alpaca Paper", "PA-2")
    brokers_api.delete_broker(old.id, _req(), db, db.get(User, _USER))
    assert _status(db) == {"Webull": "connected"}
    assert env.stopped == 0


def test_only_one_broker_is_ever_connected(env):
    db = _make_session()
    a = _acct(db, "connected", "A", "N1")
    b = _acct(db, "inactive", "B", "N2")
    c = _acct(db, "inactive", "C", "N3")
    for acct in (b, c, a, b):
        brokers_api.activate_broker(acct.id, _req(), db, db.get(User, _USER))
        assert list(_status(db).values()).count("connected") == 1


def test_snaptrade_cannot_be_deactivated(env):
    db = _make_session()
    a = _acct(db, "connected", "SnapTrade", "ST-1")
    a.broker = BrokerName.SNAPTRADE
    db.commit()
    with pytest.raises(HTTPException) as exc:
        brokers_api.deactivate_broker(a.id, _req(), db, db.get(User, _USER))
    assert exc.value.status_code == 400
    assert _status(db) == {"SnapTrade": "connected"}
