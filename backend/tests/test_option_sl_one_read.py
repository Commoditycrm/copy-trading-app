"""The option-SL monitor must not spend Webull's position-read window.

QA 2026-10-06: every P&L-poller pass read positions twice within milliseconds
(the Discord stop pass, then this monitor). Webull allows two position reads
per two seconds, so any other read in that window (Positions page, auto-trim)
was refused with 429. The monitor now reuses the pass's read, and skips the
broker entirely when it has nothing to watch.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import trader_bracket_monitor as mon

USER = uuid.uuid4()
ACCT = uuid.uuid4()


@pytest.fixture
def db():
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _jsonb(type_, compiler, **kw):  # noqa: ANN001, ARG001
        return "JSON"

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (User, BrokerAccount, Order):
        m.__table__.create(eng)
    s = sessionmaker(bind=eng)()
    s.add(User(id=USER, email="t@x.com", password_hash="x", role=UserRole.TRADER, is_active=True))
    s.add(BrokerAccount(id=ACCT, user_id=USER, broker=BrokerName.FAKE, label="t",
                        encrypted_credentials="x", connection_status="connected"))
    s.commit()
    return s


@pytest.fixture
def reads(monkeypatch):
    calls = []
    adapter = SimpleNamespace(get_positions=lambda **k: calls.append(1) or [])
    monkeypatch.setattr(mon, "adapter_for", lambda acct, creds: adapter)
    monkeypatch.setattr(mon, "decrypt_json", lambda s: {})
    return calls


def _sl_entry(db, expiry):
    db.add(Order(id=uuid.uuid4(), user_id=USER, broker_account_id=ACCT, instrument_type=InstrumentType.OPTION,
                 symbol="SPY", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=D(2),
                 status=OrderStatus.FILLED, filled_quantity=D(2), stop_loss_price=D("0.40"),
                 option_expiry=expiry, option_strike=D(780), option_right=OptionRight.CALL))
    db.commit()


def test_nothing_to_watch_makes_no_broker_call(db, reads):
    assert mon.enforce_trader_option_sl(db, USER, ACCT) == []
    assert reads == []


def test_an_expired_stop_loss_entry_is_nothing_to_watch(db, reads):
    _sl_entry(db, date.today() - timedelta(days=3))
    mon.enforce_trader_option_sl(db, USER, ACCT)
    assert reads == []


def test_a_live_stop_loss_entry_reads_positions(db, reads):
    _sl_entry(db, date.today() + timedelta(days=1))
    mon.enforce_trader_option_sl(db, USER, ACCT)
    assert reads == [1]


def test_the_poller_s_own_read_is_reused(db, reads):
    _sl_entry(db, date.today() + timedelta(days=1))
    mon.enforce_trader_option_sl(db, USER, ACCT, positions=[])
    assert reads == []
