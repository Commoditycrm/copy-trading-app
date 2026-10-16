"""Assigning a position to Self hands it to the trader: the ladder's and the
channel's orders are cancelled, and nothing is placed or sold on its own."""
import uuid
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.discord_account import DiscordAccount
from app.models.discord_alert_source import DiscordAlertSource
from app.models.discord_message import DiscordMessage, DiscordMessageStatus
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import discord_position_guard as guards

USER = uuid.uuid4()
EXP = date(2026, 10, 9)


@pytest.fixture
def db():
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _jsonb(type_, compiler, **kw):  # noqa: ANN001, ARG001
        return "JSON"

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (User, DiscordAccount, DiscordAlertSource, DiscordMessage, Order, DiscordPositionGuard):
        m.__table__.create(eng)
    s = sessionmaker(bind=eng)()
    s.add(User(id=USER, email="t@x.com", password_hash="x", role=UserRole.TRADER, is_active=True))
    s.commit()
    return s


def _src(db, channel_id, label):
    src = DiscordAlertSource(user_id=USER, label=label, channel_id=channel_id, status="connected")
    db.add(src); db.commit()
    return src


def _order(db, side=OrderSide.SELL, otype=OrderType.LIMIT, status=OrderStatus.SUBMITTED, via=None):
    o = Order(id=uuid.uuid4(), user_id=USER, instrument_type=InstrumentType.OPTION, symbol="SPY",
              side=side, order_type=otype, quantity=D(4), status=status, is_closing=side == OrderSide.SELL,
              option_expiry=EXP, option_strike=D(776), option_right=OptionRight.CALL)
    db.add(o); db.flush()
    if via is not None:
        db.add(DiscordMessage(source_id=via.id, user_id=USER, discord_message_id=str(uuid.uuid4().int)[:18],
                              discord_channel_id=via.channel_id, content="x",
                              status=DiscordMessageStatus.ORDER_CREATED, order_id=o.id))
    db.commit()
    return o


def _guard(db, **kw):
    g = DiscordPositionGuard(user_id=USER, symbol="SPY", option_strike=D(776), option_right="call",
                             option_expiry=EXP, entry_price=D("0.9775"), sell_count=1, **kw)
    db.add(g); db.commit()
    return g


def test_handing_to_self_cancels_the_ladders_and_the_channels_orders(db):
    clint = _src(db, "123", "Clint")
    tp, linked, stop = _order(db), _order(db, otype=OrderType.STOP), _order(db, otype=OrderType.STOP)
    alert_trim = _order(db, via=clint)                        # a resting trim an alert placed
    by_hand = _order(db)                                      # the trader's own limit sell
    g = _guard(db, stop_price=D("0.73"), stop_order_id=stop.id, tp_order_id=tp.id,
               tp_stop_order_id=linked.id, tp_rung=2, stop_trail_pct=D(10), stop_peak=D("1.2"))
    cancelled = []

    def _cancel(oid):
        cancelled.append(oid)
        db.get(Order, oid).status = OrderStatus.CANCELED

    n = guards.hand_to_trader(db, SimpleNamespace(id=USER), g, _cancel)
    assert set(cancelled) == {tp.id, linked.id, stop.id, alert_trim.id} and n == 4
    assert by_hand.status is OrderStatus.SUBMITTED                  # yours: left alone
    assert (g.stop_price, g.stop_trail_pct, g.tp_order_id, g.tp_off, g.fill_stop_done) == (None, None, None, True, True)


def test_a_self_position_is_manual_and_a_channel_one_is_not(db):
    me, clint = _src(db, "self", "Self"), _src(db, "123", "Clint")
    assert guards.is_manual(db, _guard(db, source_id=me.id)) is True
    assert guards.is_manual(db, SimpleNamespace(source_id=clint.id)) is False
    assert guards.is_manual(db, SimpleNamespace(source_id=None)) is False


def test_no_on_fill_stop_for_a_self_position(db):
    from app.services import discord_auto_trim

    me = _src(db, "self", "Self")
    entry = _order(db, side=OrderSide.BUY, status=OrderStatus.FILLED)
    g = _guard(db, source_id=me.id, entry_order_id=entry.id, fill_stop_done=False)
    g.sell_count = 0
    ts = SimpleNamespace(discord_fill_stop_pct=D(-25), discord_manual_exit=False, discord_stop_trails=None)
    assert discord_auto_trim.apply_fill_stop(db, g, ts) is False and g.stop_price is None
