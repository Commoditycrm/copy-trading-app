"""The position summary (fills in order, Rem.Qty after each) and Pause ALL channels."""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.broker_account import BrokerAccount, BrokerName
from app.models.discord_account import DiscordAccount
from app.models.discord_alert_source import DiscordAlertSource
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import position_history

USER, ACCT = uuid.uuid4(), uuid.uuid4()
EXP = date(2026, 10, 9)
T0 = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _jsonb(type_, compiler, **kw):  # noqa: ANN001, ARG001
        return "JSON"

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (User, BrokerAccount, Order, DiscordAccount, DiscordAlertSource):
        m.__table__.create(eng)
    s = sessionmaker(bind=eng)()
    s.add(User(id=USER, email="t@x.com", password_hash="x", role=UserRole.TRADER, is_active=True))
    s.add(BrokerAccount(id=ACCT, user_id=USER, broker=BrokerName.WEBULL, label="wb", encrypted_credentials="x"))
    s.commit()
    return s


def _fill(db, side, qty, price, minute, strike="781", filled=None):
    o = Order(id=uuid.uuid4(), user_id=USER, broker_account_id=ACCT, instrument_type=InstrumentType.OPTION,
              symbol="SPY", side=side, order_type=OrderType.LIMIT, quantity=D(qty),
              status=OrderStatus.FILLED, filled_quantity=D(filled if filled is not None else qty),
              filled_avg_price=D(price), is_closing=side == OrderSide.SELL,
              broker_filled_at=T0 + timedelta(minutes=minute),
              option_expiry=EXP, option_strike=D(strike), option_right=OptionRight.CALL)
    db.add(o); db.commit()
    return o


def _hold(db, **kw):
    return position_history.holding(db, USER, ACCT, "SPY", strike=D(781), right="call", expiry=EXP, **kw)


def test_fills_in_order_with_the_remaining_quantity(db):
    _fill(db, OrderSide.SELL, 5, "5.79", 10)           # inserted out of order on purpose
    _fill(db, OrderSide.BUY, 10, "5.20", 0)
    rows = _hold(db)
    assert [(r["side"], r["quantity"], r["price"], r["remaining"]) for r in rows] == [
        ("buy", "10", "5.2", "10"), ("sell", "5", "5.79", "5")]


def test_an_earlier_holding_of_the_same_contract_is_not_mixed_in(db):
    _fill(db, OrderSide.BUY, 2, "1.00", 0)
    _fill(db, OrderSide.SELL, 2, "1.50", 5)             # flat
    _fill(db, OrderSide.BUY, 4, "2.00", 30)             # opened again
    assert [r["quantity"] for r in _hold(db)] == ["4"]


def test_a_closing_order_returns_the_holding_it_closed(db):
    _fill(db, OrderSide.BUY, 2, "1.00", 0)
    close = _fill(db, OrderSide.SELL, 2, "1.50", 5)
    _fill(db, OrderSide.BUY, 4, "2.00", 30)
    rows = _hold(db, through_order_id=close.id)
    assert [(r["side"], r["remaining"]) for r in rows] == [("buy", "2"), ("sell", "0")]


def test_a_fully_sold_position_has_no_open_holding(db):
    _fill(db, OrderSide.BUY, 2, "1.00", 0)
    _fill(db, OrderSide.SELL, 2, "1.50", 5)
    assert _hold(db) == []


def test_only_this_contract(db):
    _fill(db, OrderSide.BUY, 2, "1.00", 0, strike="782")
    assert _hold(db) == []


def test_a_partial_fill_counts_what_filled(db):
    _fill(db, OrderSide.BUY, 4, "1.00", 0)
    _fill(db, OrderSide.SELL, 2, "1.40", 5, filled=1)
    assert _hold(db)[-1]["remaining"] == "3"


# ── Pause ALL channels ───────────────────────────────────────────────────────

def _channel(db, on=True, channel_id=None):
    src = DiscordAlertSource(user_id=USER, label="c", channel_id=channel_id or str(uuid.uuid4().int)[:12],
                             status="connected" if on else "disconnected", is_enabled=on)
    db.add(src); db.commit()
    return src


def _pause(db, paused):
    from app.api.discord_sources import pause_all
    from app.schemas.discord import PauseAllIn

    user = db.get(User, USER)
    return pause_all(PauseAllIn(paused=paused), SimpleNamespace(headers={}, client=None), db, user)


def test_pause_turns_off_every_channel_and_resume_only_those(db, monkeypatch):
    from app.services import audit
    monkeypatch.setattr(audit, "record", lambda *a, **k: None)
    a, b, already_off = _channel(db), _channel(db), _channel(db, on=False)
    self_ch = _channel(db, channel_id="self")
    out = _pause(db, True)
    assert out.paused is True and out.changed == 2
    assert not a.is_enabled and not b.is_enabled and a.status == "disconnected"
    assert self_ch.is_enabled                                  # the Self channel is never paused
    out = _pause(db, False)
    assert out.paused is False and out.changed == 2
    assert a.is_enabled and b.is_enabled and not already_off.is_enabled


def test_pausing_with_nothing_on_is_not_a_pause(db, monkeypatch):
    from app.services import audit
    monkeypatch.setattr(audit, "record", lambda *a, **k: None)
    _channel(db, on=False)
    assert _pause(db, True).paused is False
