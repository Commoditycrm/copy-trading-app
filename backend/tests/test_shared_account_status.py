"""A quarantined trader is told why, instead of a bare "Broker offline".

QA 2026-10-21: trader Revathi connected the Alpaca account her test subscriber
already had. PR #467 rightly kept her listener off and refused the follow — but
her pill just said "Broker offline", orders she placed in Alpaca silently
didn't appear, and nothing said the account was the reason.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api import brokers as brokers_api
from app.api import follow_requests as follow_api
from app.api import listener as listener_api
from app.api import settings as settings_api
from app.brokers.base import ConnectionInfo
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.follow_request import FollowRequest, FollowRequestStatus
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.schemas.broker import AlpacaCredentialsIn, ConnectBrokerIn
from app.schemas.settings import FollowTraderIn
from app.services import listener_state, listeners, shared_accounts

from test_broker_connect_replace import _USER, _make_session  # noqa: E402

_TRADER = uuid.UUID("d1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5a")


# ── real Postgres + Redis: status, reconcile ──────────────────────────────


@pytest.fixture
def pg():
    made: list = []

    def user(role):
        with SessionLocal() as db:
            u = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@t.local", password_hash="x", role=role)
            db.add(u)
            db.commit()
            made.append(u.id)
            return u.id

    def acct(uid, number, status="connected"):
        with SessionLocal() as db:
            a = BrokerAccount(id=uuid.uuid4(), user_id=uid, broker=BrokerName.ALPACA, label="t",
                              encrypted_credentials="x", broker_account_number=number,
                              connection_status=status)
            db.add(a)
            db.commit()
            return a.id

    yield SimpleNamespace(user=user, acct=acct)
    for uid in made:
        listener_state.clear(uid)
    with SessionLocal() as db:
        db.execute(delete(BrokerAccount).where(BrokerAccount.user_id.in_(made)))
        db.execute(delete(User).where(User.id.in_(made)))
        db.commit()


def test_quarantined_trader_gets_a_reason_not_just_offline(pg):
    trader = pg.user(UserRole.TRADER)
    sub = pg.user(UserRole.SUBSCRIBER)
    t_acct = pg.acct(trader, "PA-SHARED")
    pg.acct(sub, "PA-SHARED")

    assert shared_accounts.listener_blocked(t_acct)
    st = listener_state.get_status(trader)
    assert st.state == "shared_account"
    assert "another Kopyya user" in st.last_error

    # repeated reconcile ticks don't keep resetting the timestamp
    first = st.state_changed_at
    shared_accounts.listener_blocked(t_acct)
    assert listener_state.get_status(trader).state_changed_at == first


def test_unshared_trader_status_is_untouched(pg):
    trader = pg.user(UserRole.TRADER)
    t_acct = pg.acct(trader, "PA-OWN")
    assert not shared_accounts.listener_blocked(t_acct)
    assert listener_state.get_status(trader) is None


def test_listener_status_endpoint_reports_it(pg):
    trader = pg.user(UserRole.TRADER)
    sub = pg.user(UserRole.SUBSCRIBER)
    t_acct = pg.acct(trader, "PA-SHARED")
    pg.acct(sub, "PA-SHARED")
    shared_accounts.listener_blocked(t_acct)
    with SessionLocal() as db:
        out = listener_api.listener_status(db, db.get(User, trader))
    assert out["state"] == "shared_account" and out["last_error"]


def test_reconcile_keeps_the_reason_and_stops_any_listener(pg, monkeypatch):
    trader = pg.user(UserRole.TRADER)
    sub = pg.user(UserRole.SUBSCRIBER)
    pg.acct(trader, "PA-SHARED")
    pg.acct(sub, "PA-SHARED")
    stopped: list = []

    def _stop(t):
        stopped.append(t)
        listener_state.set_state(t, "disconnected")   # what a real stop writes

    monkeypatch.setattr(listeners, "_running_listener_trader_ids", lambda: {trader})
    monkeypatch.setattr(listeners, "_start_listener_local", lambda t, a: None)
    monkeypatch.setattr(listeners, "_stop_listener_local", _stop)
    monkeypatch.setattr(listeners, "_listened_brokers", lambda: [BrokerName.ALPACA])

    listeners.reconcile_once()
    assert stopped == [trader]
    assert listener_state.get_status(trader).state == "shared_account"

    # next tick: no listener running; the stale-key sweep must not erase the reason
    monkeypatch.setattr(listeners, "_running_listener_trader_ids", lambda: set())
    listeners.reconcile_once()
    assert listener_state.get_status(trader).state == "shared_account"


# ── SQLite endpoint level: Revathi's sequence ─────────────────────────────


@pytest.fixture
def env(monkeypatch):
    calls = SimpleNamespace(number="PA-SHARED")

    class _Adapter:
        def verify_connection(self):
            return ConnectionInfo(broker_account_id=calls.number, supports_fractional=True, extra={})

    monkeypatch.setattr(brokers_api, "adapter_for", lambda acct, creds: _Adapter())
    monkeypatch.setattr(brokers_api, "_refresh_balance_into", lambda acct, creds: None)
    monkeypatch.setattr(brokers_api.cache, "invalidate_broker_accounts", lambda uid: None)
    monkeypatch.setattr(brokers_api.listeners, "stop_listener", lambda uid: None)
    monkeypatch.setattr(brokers_api, "_start_trader_listener", lambda user, acct: None)
    monkeypatch.setattr(settings_api.cache, "invalidate_subscribers_for_trader", lambda *a: None)
    monkeypatch.setattr(follow_api.cache, "invalidate_subscribers_for_trader", lambda *a: None)
    return calls


def _db():
    db = _make_session()            # _USER = subscriber ("Test32")
    eng = db.get_bind()
    TraderSettings.__table__.create(eng)
    FollowRequest.__table__.create(eng)
    db.add(User(id=_TRADER, email="rev@example.com", password_hash="x", role=UserRole.TRADER, is_active=True))
    db.add(TraderSettings(user_id=_TRADER, auto_approve_follows=False))
    db.add(SubscriberSettings(user_id=_USER, following_trader_id=None, copy_enabled=True))
    db.commit()
    return db


def _sub_acct(db, number):
    db.add(BrokerAccount(user_id=_USER, broker=BrokerName.ALPACA, label="sub", is_paper=True,
                         encrypted_credentials=brokers_api.encrypt_json({"api_key": "k", "api_secret": "s"}),
                         broker_account_number=number, connection_status="connected"))
    db.commit()


def _req():
    return SimpleNamespace(headers={}, client=None)


def _connect(db, user_id):
    return brokers_api.connect(
        ConnectBrokerIn(broker=BrokerName.ALPACA, label="Alpaca",
                        alpaca=AlpacaCredentialsIn(api_key="key12345", api_secret="secret12345", paper=True)),
        _req(), db, db.get(User, user_id))


def test_trader_connecting_an_account_someone_else_has_is_told_at_connect(env):
    db = _db()
    _sub_acct(db, "PA-SHARED")                   # not following her: allowed, but explain
    out = _connect(db, _TRADER)
    assert out.connection_status == "connected"
    assert out.notice and "another Kopyya user" in out.notice


def test_trader_with_an_account_of_her_own_gets_no_notice(env):
    db = _db()
    _sub_acct(db, "PA-SUB")
    env.number = "PA-HERS"
    assert _connect(db, _TRADER).notice is None


def test_revathi_sequence_shared_account_refuses_both_approval_paths(env):
    db = _db()
    _sub_acct(db, "PA-SHARED")
    _connect(db, _TRADER)
    fr = FollowRequest(subscriber_id=_USER, trader_id=_TRADER, status=FollowRequestStatus.PENDING)
    db.add(fr)
    db.commit()
    with pytest.raises(HTTPException) as e:          # manual approve
        follow_api._decide(fr.id, approve=True, request=_req(), background=None, db=db,
                           user=db.get(User, _TRADER))
    assert e.value.status_code == 409
    db.get(TraderSettings, _TRADER).auto_approve_follows = True
    db.commit()
    with pytest.raises(HTTPException) as e:          # auto-approve (direct follow)
        settings_api.follow_trader(FollowTraderIn(trader_id=_TRADER), _req(), db, db.get(User, _USER))
    assert e.value.status_code == 409


def test_revathi_sequence_with_separate_accounts_approves_and_follows(env):
    db = _db()
    _sub_acct(db, "PA-TEST32")
    env.number = "PA-REVATHI"
    _connect(db, _TRADER)
    db.get(TraderSettings, _TRADER).auto_approve_follows = True
    db.commit()
    out = settings_api.follow_trader(FollowTraderIn(trader_id=_TRADER), _req(), db, db.get(User, _USER))
    assert out.following_trader_id == _TRADER
