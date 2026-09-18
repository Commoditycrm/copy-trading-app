"""Regression guard: a FAILED broker connect must not disconnect the working one.

`POST /api/brokers` is replace-on-connect (one broker per user). It used to
evict the existing BrokerAccount rows FIRST and verify the new credentials
second — and the failure handler's `db.commit()`, written to persist the
`broker.connect_failed` audit row, also committed those pending DELETEs. So a
rejected attempt silently disconnected the user's working broker: copy trading
stopped with `skipped_no_broker`, and nothing told them.

Direct Webull turned that from rare into routine — its first connect normally
fails while the user approves the 2FA push in the Webull app, and the RETRY is
the one that succeeds. So the common path was "try Webull, lose SnapTrade".

These tests state the two halves of the contract:
  * connect FAILS  → the existing account survives, untouched;
  * connect SUCCEEDS → the existing account is replaced, as designed.

Real in-memory SQLite (StaticPool). No broker, no network, no Redis.
"""
import os
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
    """audit_logs.metadata_json is JSONB, which SQLite can't render. We need the
    table for real here (the bug WAS the audit commit), so render it as TEXT —
    JSONB subclasses sqltypes.JSON, so values still round-trip through the
    dialect's JSON serializer."""
    return "TEXT"


# Fixed, and containing hex letters: SQLite gives the UUID column NUMERIC
# affinity, so an all-digit uuid hex round-trips as a float and breaks
# db.get(User, ...). uuid4() hits that about once in five million — rare enough
# to look like a flake rather than a cause.
_USER = uuid.UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")


class _FakeRequest:
    """Only client_ip(request) touches this."""
    headers: dict = {}
    client = None


def _make_session():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    User.__table__.create(eng)
    BrokerAccount.__table__.create(eng)
    AuditLog.__table__.create(eng)
    # BrokerAccount.orders is a relationship, so deleting an account makes
    # SQLAlchemy read `orders` to null out broker_account_id (ON DELETE SET NULL
    # keeps history alive across a broker swap). The table has to exist.
    Order.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    db.add(User(
        id=_USER, email="sub@example.com", password_hash="x",
        role=UserRole.SUBSCRIBER, is_active=True,
    ))
    db.commit()
    return db


def _existing_broker(db, label="SnapTrade (working)") -> uuid.UUID:
    acct = BrokerAccount(
        id=uuid.uuid4(), user_id=_USER, broker=BrokerName.SNAPTRADE, label=label,
        is_paper=False, supports_fractional=True,
        encrypted_credentials=brokers_api.encrypt_json({"snaptrade_user_id": "u"}),
        connection_status="connected",
    )
    db.add(acct)
    db.commit()
    return acct.id


def _payload() -> ConnectBrokerIn:
    """A well-formed direct-broker connect. Alpaca rather than Webull so the
    test doesn't depend on webull_direct_enabled being on in this environment —
    the replace-on-connect path under test is broker-agnostic."""
    return ConnectBrokerIn(
        broker=BrokerName.ALPACA, label="New account",
        alpaca=AlpacaCredentialsIn(api_key="key12345", api_secret="secret12345", paper=True),
    )


class _Patched:
    """Stub out everything connect() reaches outside the DB: the broker call,
    the balance pull, the Redis cache bust and the listener control."""

    def __init__(self, verify):
        self._verify = verify
        # Actions in the order audit.record() was CALLED. Row order in the table
        # can't stand in for this — ids are random UUIDs and created_at is a
        # same-second server default, so both rows of one commit tie.
        self.audited: list[str] = []

    def __enter__(self):
        self._saved = {
            "adapter_for": brokers_api.adapter_for,
            "_refresh_balance_into": brokers_api._refresh_balance_into,
            "cache_invalidate": brokers_api.cache.invalidate_broker_accounts,
            "stop_listener": brokers_api.listeners.stop_listener,
            "audit_record": brokers_api.audit.record,
        }
        verify = self._verify
        real_record = self._saved["audit_record"]
        audited = self.audited

        class _Adapter:
            def verify_connection(self):
                return verify()

        def _record(db, **kw):
            audited.append(kw["action"])
            return real_record(db, **kw)

        brokers_api.adapter_for = lambda acct, creds: _Adapter()
        brokers_api._refresh_balance_into = lambda acct, creds: None
        brokers_api.cache.invalidate_broker_accounts = lambda uid: None
        brokers_api.listeners.stop_listener = lambda uid: None
        brokers_api.audit.record = _record
        return self

    def __exit__(self, *exc):
        brokers_api.adapter_for = self._saved["adapter_for"]
        brokers_api._refresh_balance_into = self._saved["_refresh_balance_into"]
        brokers_api.cache.invalidate_broker_accounts = self._saved["cache_invalidate"]
        brokers_api.listeners.stop_listener = self._saved["stop_listener"]
        brokers_api.audit.record = self._saved["audit_record"]


def _accounts(db) -> list[BrokerAccount]:
    return list(db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == _USER)
    ).scalars())


def _connect(db, verify, audited: list | None = None):
    """Run the endpoint. `audited` collects the audit actions in call order."""
    user = db.get(User, _USER)
    with _Patched(verify) as patched:
        try:
            return brokers_api.connect(_payload(), _FakeRequest(), db, user)
        finally:
            if audited is not None:
                audited.extend(patched.audited)


# ── the regression ──────────────────────────────────────────────────────────
def test_failed_connect_keeps_the_existing_broker():
    db = _make_session()
    existing_id = _existing_broker(db)

    def _boom():
        raise RuntimeError("Webull token not verified — approve the 2FA prompt")

    try:
        _connect(db, _boom)
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("expected the failed connect to raise")

    # The user still has exactly their original, still-connected broker.
    accts = _accounts(db)
    assert [a.id for a in accts] == [existing_id]
    assert accts[0].connection_status == "connected"
    assert accts[0].label == "SnapTrade (working)"


def test_failed_connect_persists_the_audit_row():
    """The failure still has to be recorded — that's what the commit was for."""
    db = _make_session()
    _existing_broker(db)
    try:
        _connect(db, lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    except HTTPException:
        pass
    db.expire_all()
    # Committed and readable after the request, not rolled back with the rest.
    actions = [a.action for a in db.execute(select(AuditLog)).scalars()]
    assert actions == ["broker.connect_failed"]
    # ...and no record of a replacement that never happened.
    assert "broker.replaced" not in actions


def test_failed_connect_creates_no_ghost_row():
    db = _make_session()
    try:
        _connect(db, lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    except HTTPException:
        pass
    assert _accounts(db) == []


def test_repeated_failures_still_leave_the_broker_intact():
    """The realistic Webull shape: several rejected attempts, then a success."""
    db = _make_session()
    existing_id = _existing_broker(db)
    for _ in range(3):
        try:
            _connect(db, lambda: (_ for _ in ()).throw(RuntimeError("2FA pending")))
        except HTTPException:
            pass
        assert [a.id for a in _accounts(db)] == [existing_id]


# ── the designed behaviour still holds ──────────────────────────────────────
def test_successful_connect_replaces_the_existing_broker():
    db = _make_session()
    existing_id = _existing_broker(db)
    acct = _connect(db, lambda: ConnectionInfo(
        broker_account_id="ACCT-123", supports_fractional=False, extra={},
    ))
    accts = _accounts(db)
    assert len(accts) == 1
    assert accts[0].id == acct.id != existing_id
    assert accts[0].broker == BrokerName.ALPACA
    assert accts[0].connection_status == "connected"
    assert accts[0].broker_account_number == "ACCT-123"
    assert accts[0].supports_fractional is False   # taken from ConnectionInfo


def test_successful_connect_audits_replaced_then_connected():
    """Eviction still happens before the new row is audited, so the trail reads
    replaced -> connected rather than the reverse."""
    db = _make_session()
    _existing_broker(db)
    actions: list[str] = []
    _connect(db, lambda: ConnectionInfo(
        broker_account_id="A", supports_fractional=True, extra={},
    ), audited=actions)
    assert actions == ["broker.replaced", "broker.connected"]


def test_first_ever_connect_works_with_nothing_to_replace():
    db = _make_session()
    acct = _connect(db, lambda: ConnectionInfo(
        broker_account_id="A", supports_fractional=True, extra={},
    ))
    assert [a.id for a in _accounts(db)] == [acct.id]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll broker connect-replace tests passed.")
