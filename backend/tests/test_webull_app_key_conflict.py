"""Webull allows ONE live events subscription per app key.

Within one environment, connecting or activating a Webull account deactivates
any other connected Webull account on the same key — any user's — with a toast
notice. Across environments (QA / local / prod share nothing) the listener
can't release the other side, so it reports "in_use_elsewhere" plainly and
keeps checking until the key is free.
"""
import asyncio
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import brokers as brokers_api
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.user import User, UserRole
from app.services import listener_state, webull_listener

from test_broker_activate import _req, env  # noqa: E402,F401 — fixture
from test_broker_connect_replace import _USER, _make_session  # noqa: E402

OTHER = uuid.uuid4()


@pytest.fixture
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr(brokers_api, "create_notification",
                        lambda db, **kw: sent.append(kw))
    return sent


def _webull(db, user_id, status, number, key="KEY-1"):
    a = BrokerAccount(
        user_id=user_id, broker=BrokerName.WEBULL, label=f"Webull {number}", is_paper=True,
        encrypted_credentials=brokers_api.encrypt_json(
            {"app_key": key, "app_secret": "s", "account_id": number, "paper": True}),
        broker_account_number=number, connection_status=status,
    )
    db.add(a); db.commit()
    return a


def _db_with_other_user():
    db = _make_session()
    db.add(User(id=OTHER, email="other@example.com", password_hash="x",
                role=UserRole.TRADER, is_active=True))
    db.commit()
    return db


def _status(db):
    return {a.label: a.connection_status
            for a in db.execute(select(BrokerAccount)).scalars()}


def test_activating_releases_another_users_account_on_the_same_key(env, notes):
    db = _db_with_other_user()
    theirs = _webull(db, OTHER, "connected", "WB-THEIRS")
    mine = _webull(db, _USER, "inactive", "WB-MINE")
    out = brokers_api.activate_broker(mine.id, _req(), db, db.get(User, _USER))
    assert _status(db) == {"Webull WB-THEIRS": "inactive", "Webull WB-MINE": "connected"}
    assert "WB-THEIRS" in out.notice
    assert env.stopped >= 1                       # their listener freed the stream
    assert notes and notes[0]["user_id"] == OTHER
    assert db.get(BrokerAccount, theirs.id).encrypted_credentials   # keys kept


def test_a_different_key_is_left_alone(env, notes):
    db = _db_with_other_user()
    _webull(db, OTHER, "connected", "WB-THEIRS", key="KEY-2")
    mine = _webull(db, _USER, "inactive", "WB-MINE")
    out = brokers_api.activate_broker(mine.id, _req(), db, db.get(User, _USER))
    assert _status(db) == {"Webull WB-THEIRS": "connected", "Webull WB-MINE": "connected"}
    assert out.notice is None
    assert notes == []


def test_inactive_accounts_on_the_key_are_not_touched(env, notes):
    db = _db_with_other_user()
    _webull(db, OTHER, "inactive", "WB-THEIRS")
    mine = _webull(db, _USER, "inactive", "WB-MINE")
    out = brokers_api.activate_broker(mine.id, _req(), db, db.get(User, _USER))
    assert out.notice is None
    assert notes == []


def test_connect_releases_the_key_too(env, notes, monkeypatch):
    db = _db_with_other_user()
    _webull(db, OTHER, "connected", "WB-THEIRS")
    monkeypatch.setattr(brokers_api, "_credentials_for", lambda payload, uid: {
        "app_key": "KEY-1", "app_secret": "s", "account_id": "PA-2", "paper": True})
    payload = SimpleNamespace(broker=BrokerName.WEBULL, label="Webull Paper")
    out = brokers_api.connect(payload, _req(), db, db.get(User, _USER))
    assert _status(db)["Webull WB-THEIRS"] == "inactive"
    assert out.connection_status == "connected"
    assert "WB-THEIRS" in out.notice


# ── the listener, when the key is live in another environment ──────────────

class _Refused(Exception):
    pass


def test_the_listener_reports_in_use_elsewhere_and_keeps_checking(monkeypatch):
    tid, aid = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(webull_listener, "_load_creds",
                        lambda _aid: {"app_key": "KEY-1", "account_id": "A1"})

    class _Client:
        def do_subscribe(self, ids):
            raise _Refused('status = StatusCode.RESOURCE_EXHAUSTED\n\tdetails = '
                           '"appKey already has an active subscription"')

    monkeypatch.setattr(webull_listener, "_build_stoppable_client", lambda creds: _Client())
    monkeypatch.setattr(webull_listener, "_all_account_ids", lambda creds: ["A1"])
    monkeypatch.setattr(listener_state, "_mirror_to_redis", lambda *a: None)
    monkeypatch.setattr(listener_state, "_broadcast_state_changed", lambda *a: None)
    sleeps = []

    async def _sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(webull_listener.asyncio, "sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(webull_listener._run_listener(tid, aid))

    st = listener_state.get_status(tid)
    assert st.state == webull_listener.IN_USE_ELSEWHERE
    assert st.last_error == webull_listener.IN_USE_MESSAGE
    assert sleeps == [30.0, 30.0]          # a steady re-check, not a backoff storm


def test_other_errors_still_reconnect():
    assert not webull_listener._in_use_elsewhere(RuntimeError("socket closed"))
    assert webull_listener._in_use_elsewhere(
        RuntimeError('details = "appKey already has an active subscription"'))
