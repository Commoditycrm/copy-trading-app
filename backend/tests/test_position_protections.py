"""What protects each position, for the Positions page icons and their details."""
import uuid
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.broker_account import BrokerAccount, BrokerName
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import position_protections as pp

USER, ACCT = uuid.uuid4(), uuid.uuid4()
EXP = date(2026, 10, 9)


@pytest.fixture
def db():
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _jsonb(type_, compiler, **kw):  # noqa: ANN001, ARG001
        return "JSON"

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (User, BrokerAccount, Order, DiscordPositionGuard):
        m.__table__.create(eng)
    s = sessionmaker(bind=eng)()
    s.add(User(id=USER, email="t@x.com", password_hash="x", role=UserRole.TRADER, is_active=True))
    s.add(BrokerAccount(id=ACCT, user_id=USER, broker=BrokerName.WEBULL, label="wb", encrypted_credentials="x"))
    s.commit()
    return s


def _pos(qty=2, strike="781"):
    return SimpleNamespace(symbol="SPY", option_strike=D(strike), option_right=OptionRight.CALL,
                           option_expiry=EXP, quantity=D(qty), broker_account_id=ACCT)


def _order(db, otype, *, side=OrderSide.SELL, closing=True, stop=None, limit=None, qty=2, **kw):
    o = Order(id=uuid.uuid4(), user_id=USER, broker_account_id=ACCT, instrument_type=InstrumentType.OPTION,
              symbol="SPY", side=side, order_type=otype, quantity=D(qty), status=OrderStatus.SUBMITTED,
              stop_price=D(stop) if stop else None, limit_price=D(limit) if limit else None, is_closing=closing,
              option_expiry=EXP, option_strike=D(781), option_right=OptionRight.CALL, **kw)
    db.add(o); db.commit()
    return o


def _guard(db, **kw):
    g = DiscordPositionGuard(user_id=USER, symbol="SPY", option_strike=D(781), option_right="call",
                             option_expiry=EXP, entry_price=D("2.00"), sell_count=1, **kw)
    db.add(g); db.commit()
    return g


def _run(db, *positions):
    pp.attach(db, USER, list(positions))
    return positions


def test_nothing_placed_is_an_empty_list(db):
    (p,) = _run(db, _pos())
    assert p.protections == []


def test_a_trailing_ladder_stop_and_its_take_profit(db):
    sl = _order(db, OrderType.STOP, stop="2.23")
    tp = _order(db, OrderType.LIMIT, limit="2.40", qty=1)
    _guard(db, stop_price=D("2.23"), stop_trail_pct=D(15), stop_peak=D("2.62"),
           tp_order_id=tp.id, tp_stop_order_id=sl.id, tp_rung=2)
    (p,) = _run(db, _pos())
    kinds = {i["kind"]: i for i in p.protections}
    assert set(kinds) == {"trailing_stop", "take_profit"}       # each order listed once
    ts = kinds["trailing_stop"]
    assert ts["price"] == "2.23" and ts["trail_pct"] == "15" and ts["peak"] == "2.62"
    assert ts["where"] == "Webull" and ts["note"] == "linked to the take-profit"
    assert kinds["take_profit"]["price"] == "2.4" and kinds["take_profit"]["note"] == "Trim 2"


def test_an_emulated_ladder_stop_is_watched_by_the_app(db):
    _guard(db, stop_price=D("1.50"))
    (p,) = _run(db, _pos())
    assert p.protections[0]["kind"] == "stop" and p.protections[0]["where"] == "app"


def test_a_stop_order_placed_any_other_way(db):
    _order(db, OrderType.STOP, stop="1.80")
    (p,) = _run(db, _pos())
    assert [(i["kind"], i["price"], i["source"]) for i in p.protections] == [("stop", "1.8", "order")]


def test_a_plain_limit_sell_is_not_a_target_but_a_closing_one_is(db):
    _order(db, OrderType.LIMIT, limit="3.00", closing=False)
    (p,) = _run(db, _pos())
    assert p.protections == []
    _order(db, OrderType.LIMIT, limit="3.10", closing=True)
    (p,) = _run(db, _pos())
    assert [i["kind"] for i in p.protections] == ["take_profit"]


def test_another_contract_s_orders_stay_on_their_own_row(db):
    _order(db, OrderType.STOP, stop="1.80")
    a, b = _run(db, _pos(strike="781"), _pos(strike="782"))
    assert len(a.protections) == 1 and b.protections == []


def test_the_entry_bracket_stop_when_nothing_rests_for_it(db):
    db.add(Order(id=uuid.uuid4(), user_id=USER, broker_account_id=ACCT, instrument_type=InstrumentType.OPTION,
                 symbol="SPY", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=D(2),
                 status=OrderStatus.FILLED, is_closing=False, stop_loss_price=D("1.40"),
                 option_expiry=EXP, option_strike=D(781), option_right=OptionRight.CALL))
    db.commit()
    (p,) = _run(db, _pos())
    assert [(i["kind"], i["price"], i["note"]) for i in p.protections] == [("stop", "1.4", "entry's SL")]


def test_the_positions_payload_carries_them():
    from app.schemas.position import PositionOut, ProtectionOut

    out = PositionOut(broker_account_id=ACCT, broker_symbol="SPY", symbol="SPY", instrument_type="stock",
                      quantity=D(1), avg_entry_price=None, current_price=None, market_value=None,
                      unrealized_pnl=None, cost_basis=None, option_expiry=None, option_strike=None,
                      option_right=None)
    out.protections = [ProtectionOut(kind="stop", price="1.8")]
    assert out.model_dump(mode="json")["protections"][0]["kind"] == "stop"
