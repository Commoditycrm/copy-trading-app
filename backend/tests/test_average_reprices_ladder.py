"""Once an average fills, the ladder recalculates from the new average cost.

QA 2026-10-07: AMZN 257.5P entered 4 @ 0.41, averaged 4 @ 0.38 (8 held, cost
0.395) — the summary still read "Entry 0.41" and the Trim 1 take-profit stayed
at 0.50 (+20% of 0.41). The every-pass entry sync adopted only the OPENING
order's fill, so it undid each average.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import discord_position_guard as guards

USER = uuid.uuid4()
EXP = date(2026, 10, 7)
T0 = datetime(2026, 10, 7, 15, 58, tzinfo=timezone.utc)


@pytest.fixture
def db():
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _jsonb(type_, compiler, **kw):  # noqa: ANN001, ARG001
        return "JSON"

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (User, Order, DiscordPositionGuard):
        m.__table__.create(eng)
    s = sessionmaker(bind=eng)()
    s.add(User(id=USER, email="t@x.com", password_hash="x", role=UserRole.TRADER, is_active=True))
    s.commit()
    return s


def _buy(db, qty, price, minute, status=OrderStatus.FILLED):
    o = Order(id=uuid.uuid4(), user_id=USER, instrument_type=InstrumentType.OPTION, symbol="AMZN",
              side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=D(qty), status=status,
              filled_quantity=D(qty) if status == OrderStatus.FILLED else D(0),
              filled_avg_price=D(price) if status == OrderStatus.FILLED else None,
              is_closing=False, created_at=T0 + timedelta(minutes=minute),
              option_expiry=EXP, option_strike=D("257.5"), option_right=OptionRight.PUT)
    db.add(o); db.commit()
    return o


def _sell(db, qty, price, minute):
    o = Order(id=uuid.uuid4(), user_id=USER, instrument_type=InstrumentType.OPTION, symbol="AMZN",
              side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=D(qty), status=OrderStatus.FILLED,
              filled_quantity=D(qty), filled_avg_price=D(price), is_closing=True,
              created_at=T0 + timedelta(minutes=minute), broker_filled_at=T0 + timedelta(minutes=minute),
              option_expiry=EXP, option_strike=D("257.5"), option_right=OptionRight.PUT)
    db.add(o); db.commit()
    return o


def _guard(db, entry_order, **kw):
    kw.setdefault("sell_count", 0)
    g = DiscordPositionGuard(user_id=USER, symbol="AMZN", option_strike=D("257.5"), option_right="put",
                             option_expiry=EXP, entry_price=D("0.41"), entry_order_id=entry_order.id,
                             created_at=T0 - timedelta(seconds=5), **kw)
    db.add(g); db.commit()
    return g


def _ts(fill=D(-25), trim1_stop=D(-25), trails=None):
    return SimpleNamespace(discord_trim_count=3, discord_extra_trims=[], discord_stop_trails=trails,
                           discord_trim_profit_gate_pct=D(20), discord_trim_stop_pct=trim1_stop, discord_trim_qty_pct=D(50),
                           discord_trim2_profit_gate_pct=D(45), discord_trim2_stop_pct=D(1), discord_trim2_qty_pct=D(50),
                           discord_trim3_profit_gate_pct=D(75), discord_trim3_stop_pct=D(25), discord_trim3_qty_pct=D(75),
                           discord_fill_stop_pct=fill)


def test_the_entry_becomes_the_average_of_every_filled_buy(db):
    entry = _buy(db, 4, "0.41", 0)
    g = _guard(db, entry)
    _buy(db, 4, "0.38", 1)                                   # the average
    _buy(db, 4, "0.30", 2, status=OrderStatus.SUBMITTED)     # not filled: not counted
    assert guards.sync_entry_price(db, g) is True
    assert g.entry_price == D("0.395")
    assert guards.sync_entry_price(db, g) is False            # and it stays there


def test_the_ladders_stop_moves_with_the_average(db):
    entry = _buy(db, 4, "0.41", 0)
    g = _guard(db, entry, stop_price=D("0.30"))              # On Fill -25% of 0.41 = 0.3075 -> 0.30
    _buy(db, 4, "0.38", 1)
    guards.sync_entry_price(db, g, _ts())
    assert g.stop_price == D("0.29")                           # -25% of 0.395 = 0.29625 -> 0.29


def test_after_a_trim_the_stop_is_that_trims_stop_from_the_new_average(db):
    entry = _buy(db, 4, "0.41", 0)
    g = _guard(db, entry, stop_price=D("0.41"), sell_count=2)  # T2's stop +1% of 0.41 = 0.4141 -> 0.41
    _buy(db, 4, "0.38", 1)
    guards.sync_entry_price(db, g, _ts())
    assert g.stop_price == D("0.39")                           # +1% of 0.395 = 0.39895 -> 0.39


def test_a_stop_set_by_hand_is_left_alone(db):
    entry = _buy(db, 4, "0.41", 0)
    g = _guard(db, entry, stop_price=D("0.35"))               # not -25% of 0.41
    _buy(db, 4, "0.38", 1)
    guards.sync_entry_price(db, g, _ts())
    assert g.entry_price == D("0.395") and g.stop_price == D("0.35")


def test_a_trailing_stop_is_left_alone(db):
    entry = _buy(db, 4, "0.41", 0)
    g = _guard(db, entry, stop_price=D("0.40"), stop_trail_pct=D(10), stop_peak=D("0.45"))
    _buy(db, 4, "0.38", 1)
    guards.sync_entry_price(db, g, _ts())
    assert g.stop_price == D("0.40")


def test_a_buy_from_an_earlier_holding_is_not_averaged_in(db):
    _buy(db, 4, "1.00", -60)                                  # an earlier holding…
    _sell(db, 4, "1.20", -30)                                 # …closed out
    entry = _buy(db, 4, "0.41", 0)
    g = _guard(db, entry)
    guards.sync_entry_price(db, g)
    assert g.entry_price == D("0.41")


def test_the_entry_order_placed_just_before_the_guard_still_counts(db):
    """QA 2026-10-08: 6 @ 1.00 then 2 @ 0.91 read as 0.91 — the guard is created
    a moment AFTER its entry order, and the entry was left out of the average."""
    entry = _buy(db, 6, "1.00", 0)
    g = _guard(db, entry)
    g.created_at = entry.created_at + timedelta(seconds=1)       # created after the order
    db.commit()
    _buy(db, 2, "0.91", 4)
    guards.sync_entry_price(db, g)
    assert g.entry_price == D("0.9775")


def test_a_partial_sell_keeps_the_average_cost(db):
    entry = _buy(db, 4, "1.00", 0)
    g = _guard(db, entry)
    _sell(db, 2, "1.50", 5)                                   # a trim: cost per contract unchanged
    _buy(db, 2, "0.80", 10)                                   # re-entered what was trimmed
    guards.sync_entry_price(db, g)
    assert g.entry_price == D("0.9000")                       # (2 x 1.00 + 2 x 0.80) / 4


# ── a re-entry starts the ladder over ───────────────────────────────────────

def test_re_entering_after_a_full_exit_opens_a_fresh_ladder_on_the_same_channel(db):
    old_entry = _buy(db, 4, "1.00", 0)
    old = _guard(db, old_entry, sell_count=3, source_id=uuid.uuid4())
    old.closed_at, old.closed_reason = T0 + timedelta(minutes=30), "position flat (rung 3)"
    db.commit()
    re = _buy(db, 4, "0.99", 60, status=OrderStatus.SUBMITTED)
    g = guards.restart_ladder(db, USER, "AMZN", D("257.5"), OptionRight.PUT, EXP,
                              entry_order_id=re.id, entry_price=D("0.99"), added_qty=D(4), held_qty=D(0))
    assert g.id != old.id and g.closed_at is None
    assert (g.sell_count, g.entry_price, g.entry_order_id, g.source_id) == (0, D("0.99"), re.id, old.source_id)


def test_re_entering_what_a_trim_sold_resets_the_ladder_and_reprotects(db):
    entry = _buy(db, 4, "1.00", 0)
    g = _guard(db, entry, sell_count=2, stop_price=D("1.01"))
    re = _buy(db, 2, "0.80", 10, status=OrderStatus.SUBMITTED)
    out = guards.restart_ladder(db, USER, "AMZN", D("257.5"), OptionRight.PUT, EXP,
                                entry_order_id=re.id, entry_price=D("0.80"), added_qty=D(2),
                                held_qty=D(2), ts=_ts(fill=D(-25)))
    assert out.id == g.id and out.sell_count == 0
    assert out.entry_price == D("0.9000")                     # (2 x 1.00 + 2 x 0.80) / 4
    assert out.stop_price == D("0.67")                        # On Fill -25% of 0.90
    assert out.fill_stop_done is True
