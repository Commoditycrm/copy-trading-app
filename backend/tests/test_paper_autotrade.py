"""QA automatic Alpaca PAPER trading: the paper guard (identity, sharing,
order caps), when it applies, copy targets, and the admin endpoints.

Alpaca and Redis are fakes; nothing is placed anywhere. Uses the database at
DATABASE_URL for the account rows and audit records.
"""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api import paper_autotrade as api
from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter
from app.config import get_settings
from app.database import SessionLocal
from app.models.audit_log import AuditLog
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.user import User, UserRole
from app.services import app_settings, paper_autotrade as pa
from app.services.crypto import encrypt_json

PAPER = {"api_key": "PKTEST123", "api_secret": "s", "paper": True}
LIVE = {"api_key": "AKLIVE123", "api_secret": "s", "paper": False}


class FakeRedis:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def setex(self, k, ttl, v):
        self.d[k] = v

    def delete(self, k):
        self.d.pop(k, None)

    def mget(self, keys):
        return [self.d.get(k) for k in keys]

    def pipeline(self, transaction=False):
        r = self

        class P:
            def __init__(self):
                self.ops = []

            def incr(self, k):
                self.ops.append(("incr", k))

            def expire(self, k, s):
                self.ops.append(("expire", k))

            def execute(self):
                out = []
                for op, k in self.ops:
                    if op == "incr":
                        r.d[k] = int(r.d.get(k) or 0) + 1
                        out.append(r.d[k])
                    else:
                        out.append(True)
                return out
        return P()


class FakeClient:
    def __init__(self, host="https://paper-api.alpaca.markets", number="PA1TESTACCT", boom=False):
        self._base_url = host
        self.number, self.boom, self.calls = number, boom, 0

    def get_account(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("down")
        return SimpleNamespace(account_number=self.number)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(pa, "_redis", lambda: r)
    pa._identity.clear()                                            # noqa: SLF001
    s = get_settings()
    monkeypatch.setattr(s, "qa_paper_autotrade_enabled", False)
    monkeypatch.setattr(s, "qa_paper_autotrade_accounts", "")
    monkeypatch.setattr(s, "qa_paper_autotrade_max_orders_per_minute", 3)
    monkeypatch.setattr(s, "qa_paper_autotrade_max_orders_per_day", 5)
    monkeypatch.setattr(app_settings, "flag", lambda key, default: default)
    yield SimpleNamespace(redis=r)


@pytest.fixture
def world(monkeypatch):
    db = SessionLocal()
    made = []

    def user(role=UserRole.TRADER):
        u = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local", password_hash="x", role=role,
                 display_name="Anitha")
        db.add(u)
        db.flush()
        made.append(u)
        return u

    def account(u, creds=PAPER, number="PA1TESTACCT", broker=BrokerName.ALPACA, paper=True, status="connected"):
        a = BrokerAccount(id=uuid.uuid4(), user_id=u.id, broker=broker, label="Alpaca", is_paper=paper,
                          encrypted_credentials=encrypt_json(creds), broker_account_number=number,
                          connection_status=status)
        db.add(a)
        db.commit()
        return a

    def listing(*accts, toggle=False):
        s = get_settings()
        monkeypatch.setattr(s, "qa_paper_autotrade_enabled", True)
        monkeypatch.setattr(s, "qa_paper_autotrade_accounts", ",".join(str(a.id) for a in accts))
        monkeypatch.setattr(app_settings, "flag", lambda key, default: toggle if key == pa.TOGGLE_KEY else default)

    yield SimpleNamespace(db=db, user=user, account=account, listing=listing)
    db.rollback()
    for u in made:
        for a in db.query(BrokerAccount).filter(BrokerAccount.user_id == u.id).all():
            db.delete(a)
    db.flush()
    for u in made:
        persisted = db.get(User, u.id)
        if persisted is not None:
            db.delete(persisted)
    db.commit()
    db.close()


# ── switches ──────────────────────────────────────────────────────────────


def test_off_by_default_and_production_can_never_turn_it_on():
    from app.config import Settings
    assert Settings.model_fields["qa_paper_autotrade_enabled"].default is False
    assert Settings.model_fields["qa_paper_autotrade_accounts"].default == ""
    assert not pa.env_enabled() and not pa.enabled()


def test_the_admin_toggle_alone_does_nothing_without_the_environment_switch(monkeypatch):
    monkeypatch.setattr(app_settings, "flag", lambda key, default: True)
    acct = uuid.uuid4()
    monkeypatch.setattr(get_settings(), "qa_paper_autotrade_accounts", str(acct))
    assert pa.toggle_on() and not pa.enabled() and not pa.guards(acct) and not pa.governs(acct)


def test_only_listed_accounts_are_guarded_and_governed(world):
    t = world.user()
    listed, other = world.account(t), world.account(world.user(), number="PA2OTHER")
    world.listing(listed, toggle=False)
    assert pa.guards(listed.id) and not pa.governs(listed.id)     # guard on, automation off
    assert not pa.guards(other.id)
    world.listing(listed, toggle=True)
    assert pa.governs(listed.id) and not pa.governs(other.id)


# ── identity ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("broker,is_paper,creds,problem", [
    (BrokerName.ALPACA, True, PAPER, None),
    (BrokerName.FAKE, True, {}, None),
    (BrokerName.ALPACA, False, PAPER, "account is not marked paper"),
    (BrokerName.ALPACA, True, LIVE, "credentials are not paper credentials"),
    (BrokerName.ALPACA, True, {**PAPER, "api_key": "AKLIVE"}, "API key is not a paper (PK…) key"),
    (BrokerName.WEBULL, False, PAPER, "not an Alpaca account"),
    (BrokerName.SNAPTRADE, False, {}, "not an Alpaca account"),
])
def test_static_checks(broker, is_paper, creds, problem):
    probs = pa.static_problems(broker, is_paper, creds)
    assert (problem in probs) if problem else probs == []


def test_a_paper_account_alpaca_confirms_passes_and_is_rechecked_only_after_the_ttl(world):
    t = world.user()
    a = world.account(t)
    c = FakeClient()
    assert pa.verify_identity(a.id, PAPER, c, now=1000.0) is None
    assert pa.verify_identity(a.id, PAPER, c, now=1030.0) is None
    assert c.calls == 1
    assert pa.verify_identity(a.id, PAPER, c, now=1000.0 + pa.IDENTITY_TTL_S + 1) is None
    assert c.calls == 2


@pytest.mark.parametrize("client,expect", [
    (FakeClient(host="https://api.alpaca.markets"), "not on the paper host"),
    (FakeClient(number="123456789"), "not a paper account"),
    (FakeClient(number="PA9DIFFERENT"), "doesn't match"),
    (FakeClient(boom=True), "couldn't confirm"),
])
def test_anything_unproven_is_refused(world, client, expect):
    t = world.user()
    a = world.account(t)
    assert expect in pa.verify_identity(a.id, PAPER, client, force=True)


def test_a_live_account_is_refused_before_alpaca_is_asked(world):
    t = world.user()
    a = world.account(t, creds=LIVE, paper=False, number="123")
    c = FakeClient()
    assert "not marked paper" in pa.verify_identity(a.id, LIVE, c, force=True)
    assert c.calls == 0


def test_a_shared_brokerage_account_is_refused(world):
    a = world.account(world.user())
    world.account(world.user(role=UserRole.SUBSCRIBER))                # same PA number, another user
    assert "shared" in pa.verify_identity(a.id, PAPER, FakeClient(), force=True)


# ── order caps ────────────────────────────────────────────────────────────


def test_the_per_minute_cap_trips_and_holds_until_reset(world, isolate):
    a = world.account(world.user())
    at = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)
    assert [pa.count_and_cap(a.id, at) for _ in range(3)] == [None, None, None]
    assert "in one minute" in pa.count_and_cap(a.id, at)
    later = datetime(2026, 10, 23, 14, 5, tzinfo=timezone.utc)
    assert "tripped" in pa.count_and_cap(a.id, later)             # still held a minute later
    pa.reset_cap(a.id)
    assert pa.count_and_cap(a.id, later) is None
    db = world.db
    db.expire_all()
    assert db.execute(select(AuditLog).where(AuditLog.action == "paper_autotrade.cap_tripped",
                                             AuditLog.entity_id == str(a.id))).first() is not None


def test_the_daily_cap(world):
    a = world.account(world.user())
    results = [pa.count_and_cap(a.id, datetime(2026, 10, 23, 14, m, tzinfo=timezone.utc)) for m in range(6)]
    assert results[:5] == [None] * 5 and "today" in results[5]


def test_no_redis_means_no_order(world, monkeypatch):
    a = world.account(world.user())
    monkeypatch.setattr(pa, "_redis", lambda: (_ for _ in ()).throw(ConnectionError("redis down")))
    monkeypatch.setattr(pa, "verify_identity", lambda *a, **k: None)
    with pytest.raises(pa.PaperGuardError, match="cap unavailable"):
        pa.check(a.id, PAPER, FakeClient())


# ── the guard on the adapter ──────────────────────────────────────────────


def test_adapter_for_attaches_the_guard_only_to_listed_accounts(world):
    t = world.user()
    listed, other = world.account(t), world.account(world.user(), number="PA2OTHER")
    assert adapter_for(listed, PAPER).paper_guard is None             # environment switch off
    world.listing(listed)
    assert adapter_for(listed, PAPER).paper_guard is not None
    assert adapter_for(other, PAPER).paper_guard is None
    assert isinstance(adapter_for(listed, PAPER), AlpacaAdapter)      # isinstance checks keep working


def test_a_refused_order_never_reaches_alpaca(world, monkeypatch):
    a = world.account(world.user())
    world.listing(a)
    adapter = adapter_for(a, PAPER)
    sent = []
    live_client = FakeClient(host="https://api.alpaca.markets")
    live_client.submit_order = lambda *x, **k: sent.append(x)
    live_client.replace_order_by_id = lambda *x, **k: sent.append(x)
    adapter._client = live_client                                     # noqa: SLF001
    req = SimpleNamespace()
    with pytest.raises(pa.PaperGuardError, match="paper host"):
        adapter.place_order(req)
    with pytest.raises(pa.PaperGuardError):
        adapter.replace_order("abc", req)
    assert sent == []


def test_a_verified_paper_order_passes_the_guard(world):
    a = world.account(world.user())
    world.listing(a)
    adapter = adapter_for(a, PAPER)
    adapter._client = FakeClient()                                    # noqa: SLF001
    adapter.paper_guard()                                             # no exception
    assert pa.counts(a.id)["this_minute"] == 1


# ── copies ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("broker,creds,ok", [
    (BrokerName.ALPACA, PAPER, True),
    (BrokerName.FAKE, {}, True),
    (BrokerName.ALPACA, LIVE, False),
    (BrokerName.WEBULL, {"paper": False}, False),
    (BrokerName.SNAPTRADE, {}, False),
    (BrokerName.IBKR, {"paper": True}, False),
    (BrokerName.ALPACA, None, False),                                  # creds unreadable
])
def test_copy_targets_are_paper_or_simulated_only(broker, creds, ok):
    assert (pa.copy_target_problem(broker, creds) is None) is ok


# ── admin endpoints ───────────────────────────────────────────────────────


def admin(world):
    return world.user(role=UserRole.ADMIN)


def test_status_lists_accounts_checks_and_switches(world):
    a = world.account(world.user())
    world.listing(a)
    out = api.status(db=world.db, _=None)
    assert out["env_enabled"] is True and out["effective"] is False
    [v] = out["accounts"]
    assert v["id"] == str(a.id) and v["problems"] == [] and v["number_masked"] == "PA***CCT"
    assert out["limits"] == {"per_minute": 3, "per_day": 5}


def test_the_toggle_is_refused_without_the_environment_switch(world):
    with pytest.raises(HTTPException) as e:
        api.toggle(api.ToggleIn(on=True), db=world.db, admin=admin(world))
    assert e.value.status_code == 403


def test_the_toggle_is_refused_while_a_listed_account_fails_its_checks(world):
    a = world.account(world.user(), creds=LIVE, paper=False, number="12345")
    world.listing(a)
    with pytest.raises(HTTPException) as e:
        api.toggle(api.ToggleIn(on=True), db=world.db, admin=admin(world))
    assert e.value.status_code == 409


def test_the_toggle_turns_on_and_is_audited(world, monkeypatch):
    a = world.account(world.user())
    world.listing(a)
    stored = {}
    monkeypatch.setattr(app_settings, "set_flag", lambda db, key, value: stored.update({key: value}))
    who = admin(world)
    assert api.toggle(api.ToggleIn(on=True), db=world.db, admin=who) == {"toggle_on": True}
    assert stored == {pa.TOGGLE_KEY: True}
    assert world.db.execute(select(AuditLog).where(AuditLog.action == "paper_autotrade.toggled",
                                                   AuditLog.actor_user_id == who.id)).first() is not None


def test_verify_calls_alpaca_read_only_and_records_the_result(world, monkeypatch):
    a = world.account(world.user())
    world.listing(a)
    monkeypatch.setattr(AlpacaAdapter, "_c", lambda self: FakeClient())
    out = api.verify(a.id, db=world.db, admin=admin(world))
    assert out["ok"] is True and out["identity"]["number_masked"] == "PA***CCT"


def test_verify_refuses_unlisted_accounts(world):
    a = world.account(world.user())
    with pytest.raises(HTTPException) as e:
        api.verify(a.id, db=world.db, admin=admin(world))
    assert e.value.status_code == 404
