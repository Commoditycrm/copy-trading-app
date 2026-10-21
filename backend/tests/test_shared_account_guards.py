"""A trader and their follower can't end up on the same brokerage account.

Every way the pairing can be made is refused with a 409 before anything is
written: connect (either side), activate (QA 2026-10-21 11:16 restarted the
copy loop by re-activating a deactivated shared account), following the
trader, and the trader approving the follow. Pairings that already exist are
caught at runtime instead — see test_self_mirror_loop.py.
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
from app.api import follow_requests as follow_api
from app.api import settings as settings_api
from app.brokers.base import ConnectionInfo
from app.models.audit_log import AuditLog
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.follow_request import FollowRequest, FollowRequestStatus
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.schemas.broker import AlpacaCredentialsIn, ConnectBrokerIn
from app.schemas.settings import FollowTraderIn
from app.services import shared_accounts

from test_broker_connect_replace import _USER, _make_session  # noqa: E402

# hex letters on purpose: SQLite reads an all-digit uuid hex back as a number
_TRADER = uuid.UUID("b1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5e")
_OTHER_SUB = uuid.UUID("c1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5f")


@pytest.fixture
def env(monkeypatch):
    calls = SimpleNamespace(number="PA-SHARED", started=[])

    class _Adapter:
        def verify_connection(self):
            return ConnectionInfo(broker_account_id=calls.number, supports_fractional=True, extra={})

    monkeypatch.setattr(brokers_api, "adapter_for", lambda acct, creds: _Adapter())
    monkeypatch.setattr(brokers_api, "_refresh_balance_into", lambda acct, creds: None)
    monkeypatch.setattr(brokers_api.cache, "invalidate_broker_accounts", lambda uid: None)
    monkeypatch.setattr(brokers_api.listeners, "stop_listener", lambda uid: None)
    monkeypatch.setattr(brokers_api, "_start_trader_listener",
                        lambda user, acct: calls.started.append(acct.id))
    monkeypatch.setattr(settings_api.cache, "invalidate_subscribers_for_trader", lambda *a: None)
    return calls


def _db():
    db = _make_session()          # _USER is a SUBSCRIBER
    eng = db.get_bind()
    TraderSettings.__table__.create(eng)
    FollowRequest.__table__.create(eng)
    db.add(User(id=_TRADER, email="t@example.com", password_hash="x", role=UserRole.TRADER, is_active=True))
    db.add(User(id=_OTHER_SUB, email="o@example.com", password_hash="x", role=UserRole.SUBSCRIBER, is_active=True))
    db.add(TraderSettings(user_id=_TRADER, auto_approve_follows=True))
    db.commit()
    return db


def _acct(db, user_id, number, status="connected", broker=BrokerName.ALPACA):
    a = BrokerAccount(
        user_id=user_id, broker=broker, label=f"{number}-{status}", is_paper=True,
        encrypted_credentials=brokers_api.encrypt_json({"api_key": "k", "api_secret": "s", "paper": True}),
        broker_account_number=number, connection_status=status,
    )
    db.add(a)
    db.commit()
    return a


def _follow(db, sub_id, trader_id=_TRADER, copy=True):
    db.add(SubscriberSettings(user_id=sub_id, following_trader_id=trader_id, copy_enabled=copy))
    db.commit()


def _req():
    return SimpleNamespace(headers={}, client=None)


def _payload():
    return ConnectBrokerIn(
        broker=BrokerName.ALPACA, label="New",
        alpaca=AlpacaCredentialsIn(api_key="key12345", api_secret="secret12345", paper=True),
    )


def _refused(db):
    return [a.action for a in db.execute(select(AuditLog)).scalars() if a.action == "broker.connect_refused"]


# ── connect ───────────────────────────────────────────────────────────────


def test_subscriber_cannot_connect_the_followed_traders_account(env):
    db = _db()
    _acct(db, _TRADER, "PA-SHARED")
    _follow(db, _USER)
    before = db.execute(select(BrokerAccount)).scalars().all()

    with pytest.raises(HTTPException) as e:
        brokers_api.connect(_payload(), _req(), db, db.get(User, _USER))
    assert e.value.status_code == 409 and "shared_broker_account" in e.value.detail
    assert len(db.execute(select(BrokerAccount)).scalars().all()) == len(before)
    assert _refused(db) == ["broker.connect_refused"]


def test_trader_cannot_connect_an_account_a_follower_holds(env):
    db = _db()
    _acct(db, _USER, "PA-SHARED")
    _follow(db, _USER, copy=False)          # paused followers count: they can resume
    with pytest.raises(HTTPException) as e:
        brokers_api.connect(_payload(), _req(), db, db.get(User, _TRADER))
    assert e.value.status_code == 409
    assert env.started == []                 # no listener on a refused connect


def test_a_different_account_still_connects(env):
    db = _db()
    _acct(db, _TRADER, "PA-SHARED")
    _follow(db, _USER)
    env.number = "PA-MINE"
    out = brokers_api.connect(_payload(), _req(), db, db.get(User, _USER))
    assert out.connection_status == "connected" and out.broker_account_number == "PA-MINE"


def test_two_subscribers_sharing_an_account_is_not_this_guard(env):
    # no trader on it, so no listener can read it — out of scope here
    db = _db()
    _acct(db, _OTHER_SUB, "PA-SHARED")
    _follow(db, _USER)
    out = brokers_api.connect(_payload(), _req(), db, db.get(User, _USER))
    assert out.connection_status == "connected"


def test_existing_brokers_are_untouched_when_refused(env):
    db = _db()
    mine = _acct(db, _USER, "PA-OLD")
    _acct(db, _TRADER, "PA-SHARED")
    _follow(db, _USER)
    with pytest.raises(HTTPException):
        brokers_api.connect(_payload(), _req(), db, db.get(User, _USER))
    db.expire_all()
    assert db.get(BrokerAccount, mine.id).connection_status == "connected"


# ── activate ──────────────────────────────────────────────────────────────


def test_trader_cannot_reactivate_a_shared_account(env):
    """The exact QA 11:16 sequence: deactivated trader row, subscriber still on
    the same account and following, trader clicks Activate."""
    db = _db()
    t = _acct(db, _TRADER, "PA-SHARED", status="inactive")
    _acct(db, _USER, "PA-SHARED")
    _follow(db, _USER)
    with pytest.raises(HTTPException) as e:
        brokers_api.activate_broker(t.id, _req(), db, db.get(User, _TRADER))
    assert e.value.status_code == 409
    db.expire_all()
    assert db.get(BrokerAccount, t.id).connection_status == "inactive"
    assert env.started == []


def test_subscriber_cannot_reactivate_into_the_traders_account(env):
    db = _db()
    s = _acct(db, _USER, "PA-SHARED", status="inactive")
    _acct(db, _TRADER, "PA-SHARED")
    _follow(db, _USER)
    with pytest.raises(HTTPException) as e:
        brokers_api.activate_broker(s.id, _req(), db, db.get(User, _USER))
    assert e.value.status_code == 409


def test_activate_checks_the_number_the_broker_reports_now(env):
    """The stored number can be stale; verify_connection's answer is what counts."""
    db = _db()
    t = _acct(db, _TRADER, "PA-OLDNUM", status="inactive")
    _acct(db, _USER, "PA-SHARED")
    _follow(db, _USER)
    env.number = "PA-SHARED"
    with pytest.raises(HTTPException):
        brokers_api.activate_broker(t.id, _req(), db, db.get(User, _TRADER))


def test_activate_without_a_conflict_still_works(env):
    db = _db()
    t = _acct(db, _TRADER, "PA-SHARED", status="inactive")
    _acct(db, _USER, "PA-SUB")
    _follow(db, _USER)
    out = brokers_api.activate_broker(t.id, _req(), db, db.get(User, _TRADER))
    assert out.connection_status == "connected"


# ── follow ────────────────────────────────────────────────────────────────


def test_cannot_follow_a_trader_on_the_same_account(env):
    db = _db()
    _acct(db, _TRADER, "PA-SHARED")
    _acct(db, _USER, "PA-SHARED")
    db.add(SubscriberSettings(user_id=_USER, following_trader_id=None, copy_enabled=True))
    db.commit()
    with pytest.raises(HTTPException) as e:
        settings_api.follow_trader(FollowTraderIn(trader_id=_TRADER), _req(), db, db.get(User, _USER))
    assert e.value.status_code == 409
    db.expire_all()
    assert db.get(SubscriberSettings, _USER).following_trader_id is None


def test_follow_and_unfollow_still_work_without_a_conflict(env):
    db = _db()
    _acct(db, _TRADER, "PA-T")
    _acct(db, _USER, "PA-S")
    db.add(SubscriberSettings(user_id=_USER, following_trader_id=None, copy_enabled=True))
    db.commit()
    out = settings_api.follow_trader(FollowTraderIn(trader_id=_TRADER), _req(), db, db.get(User, _USER))
    assert out.following_trader_id == _TRADER
    out = settings_api.follow_trader(FollowTraderIn(trader_id=None), _req(), db, db.get(User, _USER))
    assert out.following_trader_id is None


def test_trader_cannot_approve_a_follow_onto_their_own_account(env):
    db = _db()
    _acct(db, _TRADER, "PA-SHARED")
    _acct(db, _USER, "PA-SHARED")
    db.add(SubscriberSettings(user_id=_USER, following_trader_id=None, copy_enabled=True))
    fr = FollowRequest(subscriber_id=_USER, trader_id=_TRADER, status=FollowRequestStatus.PENDING)
    db.add(fr)
    db.commit()
    with pytest.raises(HTTPException) as e:
        follow_api._decide(fr.id, approve=True, request=_req(), background=None, db=db,
                           user=db.get(User, _TRADER))
    assert e.value.status_code == 409
    db.expire_all()
    assert db.get(FollowRequest, fr.id).status == FollowRequestStatus.PENDING
    assert db.get(SubscriberSettings, _USER).following_trader_id is None


# ── helper edge cases ─────────────────────────────────────────────────────


def test_conflict_helper_edges(env):
    db = _db()
    _acct(db, _TRADER, " pa-shared ")
    _acct(db, _TRADER, "PA-INACTIVE", status="inactive")
    _follow(db, _USER)
    sub = db.get(User, _USER)
    assert shared_accounts.connect_conflict(db, sub, BrokerName.ALPACA, "PA-SHARED")   # case + spaces
    assert not shared_accounts.connect_conflict(db, sub, BrokerName.WEBULL, "PA-SHARED")  # other broker
    assert not shared_accounts.connect_conflict(db, sub, BrokerName.ALPACA, None)
    assert not shared_accounts.connect_conflict(db, sub, BrokerName.ALPACA, "  ")
    # an inactive counterpart doesn't block — its own activation is checked instead
    assert not shared_accounts.connect_conflict(db, sub, BrokerName.ALPACA, "PA-INACTIVE")
