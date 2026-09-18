"""Per-user advisory lock around broker-connection replacement.

Connecting a broker is REPLACE-on-connect (one broker per user). Without a lock,
two concurrent calls both get past the "evict what's there" step before either
commits, and the user ends up with TWO BrokerAccount rows. That is not a
cosmetic duplicate: `copy_engine.fanout_async` iterates a subscriber's accounts
with no dedup (`for acct in sub_accounts:`), so two rows for the SAME brokerage
account means EVERY trader trade is mirrored TWICE into it. A double-click on
Connect, a retried request, or React Strict Mode double-firing an effect is
enough. `/snaptrade/finish` already had a lock; the direct-broker `POST
/api/brokers` used by Webull and Alpaca did not.

Two properties are worth pinning down, and neither needs Postgres:

  1. **The key is stable across processes.** The pre-existing SnapTrade lock
     derived its key from Python's ``hash()``, which is SALTED PER PROCESS — so
     the key differed between uvicorn workers and the lock only ever serialised
     requests that happened to land on the same one. The web tier runs
     ``--workers N``, so that is precisely the case it had to cover. This is the
     one bug here a unit test can catch outright, by asserting the key matches a
     value computed in a SEPARATE interpreter.

  2. **The lock is taken, and taken in the right place** — inside the handler,
     after verification (so a slow Webull 2FA round-trip doesn't hold a DB
     connection) but before the evict/insert that must not interleave.

Actual mutual exclusion is PostgreSQL's job and is not re-tested here.
"""
import os
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import brokers as brokers_api
from app.brokers.base import ConnectionInfo
from app.models.audit_log import AuditLog
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import Order
from app.models.user import User, UserRole
from app.schemas.broker import AlpacaCredentialsIn, ConnectBrokerIn


@compiles(JSONB, "sqlite")
def _jsonb_as_text_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ARG001
    return "TEXT"


# Must contain hex LETTERS. SQLite gives the UUID column NUMERIC affinity, so
# an all-digit uuid hex ("1111...8888") is silently stored and read back as a
# float — db.get(User, ...) then blows up inside uuid.UUID(). Fixed (not uuid4)
# so the cross-process key assertion compares a known value.
_USER = uuid.UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")


class _FakeRequest:
    headers: dict = {}
    client = None


def _make_session():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in (User, BrokerAccount, AuditLog, Order):
        model.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    db.add(User(
        id=_USER, email="sub@example.com", password_hash="x",
        role=UserRole.SUBSCRIBER, is_active=True,
    ))
    db.commit()
    return db


def _payload() -> ConnectBrokerIn:
    return ConnectBrokerIn(
        broker=BrokerName.ALPACA, label="New account",
        alpaca=AlpacaCredentialsIn(api_key="key12345", api_secret="secret12345", paper=True),
    )


# ── 1. the key must be identical in every process ───────────────────────────
def test_lock_key_is_stable_within_a_process():
    a = brokers_api._user_broker_lock_key(_USER)
    b = brokers_api._user_broker_lock_key(_USER)
    assert a == b


def test_lock_key_is_stable_ACROSS_processes():
    """The real bug. A fresh interpreter has a different hash salt, so a
    hash()-derived key would differ here — and two uvicorn workers would take
    two different locks and never serialise against each other."""
    ours = brokers_api._user_broker_lock_key(_USER)
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from app.api.brokers import _user_broker_lock_key as k;"
        "import uuid; print(k(uuid.UUID(%r)))"
        % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), str(_USER))
    )
    # PYTHONHASHSEED=random is the default, but set it explicitly so the child
    # can't inherit a pinned seed that would mask a hash()-based key.
    env = {**os.environ, "PYTHONHASHSEED": "random"}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.strip()) == ours


def test_lock_key_differs_per_user():
    other = uuid.uuid4()
    assert brokers_api._user_broker_lock_key(_USER) != brokers_api._user_broker_lock_key(other)


def test_lock_key_fits_a_signed_bigint():
    """pg_advisory_xact_lock takes a bigint — an out-of-range key errors at the
    database, which would take the whole connect down."""
    for _ in range(200):
        k = brokers_api._user_broker_lock_key(uuid.uuid4())
        assert -(2 ** 63) <= k < 2 ** 63


def test_snaptrade_finish_shares_the_same_key():
    """/finish and the direct connect must contend on ONE key — a /finish racing
    a direct connect is as dangerous as two of either. Both now route through
    _lock_user_brokers, so this is just a guard against them drifting apart."""
    import inspect
    src = inspect.getsource(brokers_api.snaptrade_finish)
    assert "_lock_user_brokers(db, user.id)" in src
    assert "hash((" not in src   # the old per-process-salted key is gone


# ── 2. the lock is taken, in the right place ────────────────────────────────
class _Recorder:
    """Replaces the handler's collaborators and records the call ORDER, so we
    can assert the lock lands between verification and the evict/insert."""

    def __init__(self, verify):
        self._verify = verify
        self.calls: list[str] = []

    def __enter__(self):
        self._saved = {
            "adapter_for": brokers_api.adapter_for,
            "_refresh_balance_into": brokers_api._refresh_balance_into,
            "_lock_user_brokers": brokers_api._lock_user_brokers,
            "_evict_existing_brokers": brokers_api._evict_existing_brokers,
            "cache_invalidate": brokers_api.cache.invalidate_broker_accounts,
            "stop_listener": brokers_api.listeners.stop_listener,
        }
        calls, verify = self.calls, self._verify
        real_evict = self._saved["_evict_existing_brokers"]

        class _Adapter:
            def verify_connection(self):
                calls.append("verify")
                return verify()

        def _lock(db, user_id):
            calls.append("lock")

        def _evict(db, user, request):
            calls.append("evict")
            return real_evict(db, user, request)

        brokers_api.adapter_for = lambda acct, creds: _Adapter()
        brokers_api._refresh_balance_into = lambda acct, creds: None
        brokers_api._lock_user_brokers = _lock
        brokers_api._evict_existing_brokers = _evict
        brokers_api.cache.invalidate_broker_accounts = lambda uid: None
        brokers_api.listeners.stop_listener = lambda uid: None
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            if name == "cache_invalidate":
                brokers_api.cache.invalidate_broker_accounts = fn
            elif name == "stop_listener":
                brokers_api.listeners.stop_listener = fn
            else:
                setattr(brokers_api, name, fn)


def _ok_verify():
    return ConnectionInfo(broker_account_id="A", supports_fractional=True, extra={})


def test_lock_is_taken_after_verify_and_before_evict():
    """Order matters in both directions: locking BEFORE verify would hold a DB
    connection across Webull's token/2FA round-trip (seconds), and locking AFTER
    evict would leave the replace itself unprotected — which is the race."""
    db = _make_session()
    with _Recorder(_ok_verify) as rec:
        brokers_api.connect(_payload(), _FakeRequest(), db, db.get(User, _USER))
    assert rec.calls == ["verify", "lock", "evict"]


def test_no_lock_is_taken_when_verification_fails():
    """A rejected connect mutates nothing, so it has no critical section to
    protect — and must not make other requests for this user queue behind it."""
    db = _make_session()
    with _Recorder(lambda: (_ for _ in ()).throw(RuntimeError("2FA pending"))) as rec:
        try:
            brokers_api.connect(_payload(), _FakeRequest(), db, db.get(User, _USER))
        except HTTPException:
            pass
    assert rec.calls == ["verify"]


# ── the invariant the lock exists to protect ────────────────────────────────
def test_sequential_connects_never_leave_two_accounts():
    """What the duplicate rows would actually cost: copy_engine iterates a
    subscriber's accounts with no dedup, so a second row doubles every mirror.
    Serialised connects must always land on exactly one."""
    db = _make_session()
    ids = []
    for _ in range(3):
        with _Recorder(_ok_verify):
            acct = brokers_api.connect(_payload(), _FakeRequest(), db, db.get(User, _USER))
        ids.append(acct.id)
    rows = list(db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == _USER)
    ).scalars())
    assert len(rows) == 1 and rows[0].id == ids[-1]   # last writer wins


def test_lock_helper_no_ops_off_postgres():
    """The helper must not blow up on SQLite — advisory locks are a PG feature
    and the tests above rely on it being a silent no-op there."""
    db = _make_session()
    brokers_api._lock_user_brokers(db, _USER)   # must not raise


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll broker connect-lock tests passed.")
