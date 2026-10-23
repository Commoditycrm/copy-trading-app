"""QA automatic Alpaca PAPER trading: the paper guard (identity, sharing),
when it applies, and copy targets.

Alpaca is a fake; nothing is placed anywhere. Uses the database at
DATABASE_URL for the account rows.
"""
import uuid
from types import SimpleNamespace

import pytest

from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter
from app.config import get_settings
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.user import User, UserRole
from app.services import paper_autotrade as pa
from app.services.crypto import encrypt_json

PAPER = {"api_key": "PKTEST123", "api_secret": "s", "paper": True}
LIVE = {"api_key": "AKLIVE123", "api_secret": "s", "paper": False}


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
    pa._identity.clear()                                            # noqa: SLF001
    s = get_settings()
    monkeypatch.setattr(s, "qa_paper_autotrade_enabled", False)
    monkeypatch.setattr(s, "qa_paper_autotrade_accounts", "")


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

    def listing(*accts):
        s = get_settings()
        monkeypatch.setattr(s, "qa_paper_autotrade_enabled", True)
        monkeypatch.setattr(s, "qa_paper_autotrade_accounts", ",".join(str(a.id) for a in accts))

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


def test_off_by_default():
    from app.config import Settings
    assert Settings.model_fields["qa_paper_autotrade_enabled"].default is False
    assert Settings.model_fields["qa_paper_autotrade_accounts"].default == ""
    assert not pa.env_enabled()


def test_listing_alone_does_nothing_without_the_environment_switch(monkeypatch):
    acct = uuid.uuid4()
    monkeypatch.setattr(get_settings(), "qa_paper_autotrade_accounts", str(acct))
    assert not pa.governs(acct)


def test_only_listed_accounts_are_governed(world):
    t = world.user()
    listed, other = world.account(t), world.account(world.user(), number="PA2OTHER")
    world.listing(listed)
    assert pa.governs(listed.id) and not pa.governs(other.id) and not pa.governs(None)


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
