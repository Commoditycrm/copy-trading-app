"""The Position summary as a timeline: orders (asked vs filled) and the stop's history."""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.broker_account import BrokerAccount, BrokerName
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.position_event import PositionEvent
from app.models.user import User, UserRole
from app.services import discord_position_guard as guards
from app.services import position_events, position_history

USER, ACCT = uuid.uuid4(), uuid.uuid4()
EXP = date(2026, 10, 9)
T0 = datetime.now(timezone.utc) - timedelta(hours=1)


@pytest.fixture
def db():
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _jsonb(type_, compiler, **kw):  # noqa: ANN001, ARG001
        return "JSON"

    position_events.install()
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (User, BrokerAccount, Order, DiscordPositionGuard, PositionEvent):
        m.__table__.create(eng)
    s = sessionmaker(bind=eng)()
    s.add(User(id=USER, email="t@x.com", password_hash="x", role=UserRole.TRADER, is_active=True))
    s.add(BrokerAccount(id=ACCT, user_id=USER, broker=BrokerName.WEBULL, label="wb", encrypted_credentials="x"))
    s.commit()
    return s


def _order(db, side, otype, qty, minute, *, filled=None, price=None, limit=None, stop=None,
           status=OrderStatus.FILLED):
    o = Order(id=uuid.uuid4(), user_id=USER, broker_account_id=ACCT, instrument_type=InstrumentType.OPTION,
              symbol="SPY", side=side, order_type=otype, quantity=D(qty), status=status,
              filled_quantity=D(filled if filled is not None else (qty if status == OrderStatus.FILLED else 0)),
              filled_avg_price=D(price) if price else None, limit_price=D(limit) if limit else None,
              stop_price=D(stop) if stop else None, is_closing=side == OrderSide.SELL,
              created_at=T0 + timedelta(minutes=minute),
              broker_filled_at=(T0 + timedelta(minutes=minute, seconds=5)) if status == OrderStatus.FILLED else None,
              option_expiry=EXP, option_strike=D(781), option_right=OptionRight.CALL)
    db.add(o); db.commit()
    return o


def _timeline(db, **kw):
    return position_history.timeline(db, USER, ACCT, "SPY", strike=D(781), right="call", expiry=EXP, **kw)


# ── the stop's history is recorded wherever it changes ──────────────────────

def _guard(db):
    g = DiscordPositionGuard(user_id=USER, symbol="SPY", option_strike=D(781), option_right="call",
                             option_expiry=EXP, entry_price=D("2.00"), sell_count=0)
    db.add(g); db.commit()
    return g


def test_set_moved_and_removed_are_recorded(db):
    g = _guard(db)
    g.stop_price = D("1.50"); db.commit()
    g.stop_price = D("2.00"); db.commit()
    g.stop_price = None; db.commit()
    kinds = [(e.kind, e.old_price, e.price) for e in db.query(PositionEvent).order_by(PositionEvent.created_at)]
    assert kinds == [("stop_set", None, D("1.5")), ("stop_moved", D("1.5"), D("2")), ("stop_removed", D("2"), None)]


def test_a_trailing_stop_records_each_raise(db):
    g = _guard(db)
    g.stop_price, g.stop_trail_pct, g.stop_peak = D("2.55"), D(15), D("3.00"); db.commit()
    assert guards.ratchet_stop(g, D("3.50")); db.commit()
    kinds = [e.kind for e in db.query(PositionEvent).order_by(PositionEvent.created_at)]
    assert kinds == ["trailing_stop_set", "trailing_stop_raised"]


def test_an_unchanged_save_records_nothing(db):
    g = _guard(db)
    g.sell_count = 1; db.commit()
    assert db.query(PositionEvent).count() == 0


# ── the timeline ─────────────────────────────────────────────────────────────

def test_orders_asked_vs_filled_with_labels_and_rem_qty(db):
    _order(db, OrderSide.BUY, OrderType.LIMIT, 4, 0, price="2.00", limit="2.05")
    _order(db, OrderSide.BUY, OrderType.MARKET, 2, 5, price="1.70")            # lower: an average
    _order(db, OrderSide.SELL, OrderType.LIMIT, 3, 20, price="2.42", limit="2.40")
    _order(db, OrderSide.SELL, OrderType.MARKET, 1, 30, price="2.50")
    rows = _timeline(db)
    assert [(r["label"], r["requested"], r["filled"], r["remaining"]) for r in rows] == [
        ("Entry", "4 @ 2.05 limit", "4 @ 2", "4"),
        ("Average", "2 @ market", "2 @ 1.7", "6"),
        ("T1", "3 @ 2.4 limit", "3 @ 2.42", "3"),
        ("T2", "1 @ market", "1 @ 2.5", "2"),
    ]


def test_a_stop_out_a_resting_trim_and_a_refused_one(db):
    _order(db, OrderSide.BUY, OrderType.LIMIT, 4, 0, price="2.00", limit="2.00")
    _order(db, OrderSide.SELL, OrderType.LIMIT, 2, 10, limit="2.60", status=OrderStatus.SUBMITTED)
    _order(db, OrderSide.SELL, OrderType.LIMIT, 2, 11, limit="2.70", status=OrderStatus.REJECTED)
    _order(db, OrderSide.SELL, OrderType.LIMIT, 2, 12, limit="2.80", status=OrderStatus.CANCELED)   # a replace
    _order(db, OrderSide.SELL, OrderType.STOP, 4, 13, stop="1.50", status=OrderStatus.SUBMITTED)    # resting stop
    out = _order(db, OrderSide.SELL, OrderType.STOP, 4, 40, price="1.48", stop="1.50")
    assert _timeline(db) == []                    # fully sold: no open holding
    rows = _timeline(db, through_order_id=out.id)
    assert [(r["label"], r["status"]) for r in rows] == [
        ("Entry", "filled"), ("Sell order", "submitted"), ("Sell order", "rejected"), ("Stopped out", "filled")]
    assert rows[-1]["remaining"] == "0"


def test_stop_events_are_merged_in_time_order(db):
    _order(db, OrderSide.BUY, OrderType.LIMIT, 2, 0, price="2.00", limit="2.00")
    db.add(PositionEvent(user_id=USER, symbol="SPY", option_strike=D(781), option_right="call", option_expiry=EXP,
                         kind="stop_set", price=D("1.50"), created_at=T0 + timedelta(minutes=1)))
    db.add(PositionEvent(user_id=USER, symbol="SPY", option_strike=D(781), option_right="call", option_expiry=EXP,
                         kind="trailing_stop_raised", old_price=D("1.50"), price=D("1.80"), trail_pct=D(15),
                         peak=D("2.12"), created_at=T0 + timedelta(minutes=8)))
    _order(db, OrderSide.SELL, OrderType.MARKET, 1, 5, price="2.10")
    rows = _timeline(db)
    assert [r["label"] for r in rows] == ["Entry", "Stop set", "T1", "Trailing stop raised"]
    assert rows[1]["detail"] == "@ 1.5"
    assert rows[3]["detail"] == "1.5 → 1.8 · 15% below high 2.12"


def test_an_earlier_holding_s_events_stay_with_it(db):
    _order(db, OrderSide.BUY, OrderType.MARKET, 1, 0, price="1.00")
    first_close = _order(db, OrderSide.SELL, OrderType.MARKET, 1, 5, price="1.20")
    db.add(PositionEvent(user_id=USER, symbol="SPY", option_strike=D(781), option_right="call", option_expiry=EXP,
                         kind="stop_set", price=D("0.80"), created_at=T0 + timedelta(minutes=2)))
    db.commit()
    _order(db, OrderSide.BUY, OrderType.MARKET, 3, 30, price="2.00")
    assert [r["label"] for r in _timeline(db)] == ["Entry"]
    assert [r["label"] for r in _timeline(db, through_order_id=first_close.id)] == ["Entry", "Stop set", "T1"]
