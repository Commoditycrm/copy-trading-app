"""Tests for the Discord trade-alert cards (Phase 1).

Covers the pure card formatter (ENTERING / CLOSING, stock + option) and the
entry-vs-close classifier, which must work off the trader's OWN prior fills
because the Alpaca listener always stores is_closing=False.
"""
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.services.discord_alerts as da
from app.models.order import (
    InstrumentType,
    Order,
    OrderSide,
    OptionRight,
    OrderStatus,
    OrderType,
)

_EXP = date(2026, 8, 10)
_T0 = datetime(2026, 8, 10, 16, 0, 0, tzinfo=timezone.utc)


def _session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    Order.__table__.create(eng)  # only the table under test
    return Session(eng)


def _opt(side, price, qty="1", *, closing=False, at=_T0, uid=None):
    return Order(
        id=uuid.uuid4(),
        user_id=uid or uuid.uuid4(),
        broker_account_id=uuid.uuid4(),
        instrument_type=InstrumentType.OPTION,
        symbol="AMD",
        option_strike=Decimal("465"),
        option_right=OptionRight.PUT,
        option_expiry=_EXP,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Decimal(qty),
        is_closing=closing,
        status=OrderStatus.FILLED,
        filled_quantity=Decimal(qty),
        filled_avg_price=Decimal(str(price)),
        created_at=at,
    )


def _stock(side, price, qty="200", *, at=_T0, uid=None):
    return Order(
        id=uuid.uuid4(),
        user_id=uid or uuid.uuid4(),
        broker_account_id=uuid.uuid4(),
        instrument_type=InstrumentType.STOCK,
        symbol="RDGT",
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
        is_closing=False,
        status=OrderStatus.FILLED,
        filled_quantity=Decimal(qty),
        filled_avg_price=Decimal(str(price)),
        created_at=at,
    )


# ── card formatter ────────────────────────────────────────────────────────────

def test_card_entering_option():
    e = da.build_card(_opt(OrderSide.BUY, "2.44"), is_closing=False)["embeds"][0]
    assert e["title"] == "🟢 ENTERING · AMD $465 PUT · 08/10"
    assert e["description"] == "1 @ $2.44 · $244"   # 2.44 × 1 × 100
    assert e["color"] == da._COLOR_ENTER


def test_card_closing_option_sell():
    e = da.build_card(_opt(OrderSide.SELL, "0.87"), is_closing=True)["embeds"][0]
    assert e["title"] == "🔴 CLOSING · AMD $465 PUT · 08/10"
    assert e["description"] == "Sold 1 @ $0.87\nPosition closed"
    assert e["color"] == da._COLOR_CLOSE


def test_card_closing_option_buy_to_cover():
    """A close can be a BUY (covering a short) → 'Bought', not 'Sold'."""
    e = da.build_card(_opt(OrderSide.BUY, "0.50"), is_closing=True)["embeds"][0]
    assert e["description"].startswith("Bought 1 @ $0.50")


def test_card_entering_stock_notional_no_x100():
    e = da.build_card(_stock(OrderSide.BUY, "1.53"), is_closing=False)["embeds"][0]
    assert e["title"] == "🟢 ENTERING · RDGT"
    assert e["description"] == "200 @ $1.53 · $306"  # stock: no ×100


def test_money_formatting_whole_vs_cents():
    assert da._fmt_money(Decimal("465")) == "$465"
    assert da._fmt_money(Decimal("1240")) == "$1,240"
    assert da._fmt_money(Decimal("2.44")) == "$2.44"
    assert da._fmt_money(None) == "—"


# ── entry vs close classification (off the trader's own prior fills) ──────────

def test_is_closing_long_roundtrip():
    """BUY to open, then SELL → the SELL is a CLOSE; the opening BUY is an ENTER."""
    db = _session()
    uid = uuid.uuid4()
    buy = _opt(OrderSide.BUY, "2.44", at=_T0, uid=uid)
    sell = _opt(OrderSide.SELL, "0.87", at=_T0 + timedelta(minutes=8), uid=uid)
    db.add_all([buy, sell]); db.flush()
    assert da._is_closing(db, buy) is False    # nothing held before → entering
    assert da._is_closing(db, sell) is True     # sells down the long → closing


def test_is_closing_short_roundtrip():
    """SELL to open (short), then BUY to cover → the BUY is a CLOSE."""
    db = _session()
    uid = uuid.uuid4()
    sell = _opt(OrderSide.SELL, "2.00", at=_T0, uid=uid)
    buy = _opt(OrderSide.BUY, "1.00", at=_T0 + timedelta(minutes=5), uid=uid)
    db.add_all([sell, buy]); db.flush()
    assert da._is_closing(db, sell) is False   # opens the short → entering
    assert da._is_closing(db, buy) is True      # covers the short → closing


def test_is_closing_respects_explicit_flag():
    """An explicit is_closing=True (SnapTrade close) is trusted without history."""
    db = _session()
    o = _opt(OrderSide.SELL, "0.87", closing=True)
    db.add(o); db.flush()
    assert da._is_closing(db, o) is True


def test_is_closing_ignores_other_contracts():
    """A prior fill on a DIFFERENT strike must not make this order look like a
    close."""
    db = _session()
    uid = uuid.uuid4()
    other = _opt(OrderSide.BUY, "3.00", at=_T0, uid=uid)
    other.option_strike = Decimal("500")           # different contract
    sell = _opt(OrderSide.SELL, "0.87", at=_T0 + timedelta(minutes=5), uid=uid)
    db.add_all([other, sell]); db.flush()
    assert da._is_closing(db, sell) is False        # no matching long → entering
