"""Synthetic option-expiry realization must be gated on broker capability.

An option lot still open past its expiry is booked as a worthless close ONLY
when the broker's fill history is authoritative (Alpaca). For a feed-less broker
(Webull direct) a still-open lot is more likely a close we never received than a
real expiry, so booking it invents a phantom loss — the gaurav case where our
realized read -$19,904 vs Webull's -$6,627. These guard that gate.

Real in-memory SQLite against the actual ORM + FIFO. No broker, no network.

Run standalone:  .venv/bin/python tests/test_expiry_broker_gating.py
Or under pytest: pytest tests/test_expiry_broker_gating.py
"""
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.brokers.capabilities import capabilities_for
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import (
    Fill, InstrumentType, Order, OrderSide, OrderStatus, OrderType, OptionRight,
)
from app.services.pnl import realized_pnl_by_day

# A past Friday, comfortably before any "today" the tests run on.
_EXPIRY = date(2026, 8, 14)
_OPEN = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)


def _session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    BrokerAccount.__table__.create(eng)
    Order.__table__.create(eng)
    Fill.__table__.create(eng)
    return Session(eng)


def _acct(db: Session, broker: BrokerName) -> BrokerAccount:
    a = BrokerAccount(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker=broker, label="t",
        is_paper=True, supports_fractional=True, encrypted_credentials="x",
        connection_status="connected",
    )
    db.add(a)
    db.flush()
    return a


def _buy_option_let_expire(db: Session, acct: BrokerAccount, side=OrderSide.BUY) -> Order:
    """Open a 2-lot option and never close it → still open past _EXPIRY."""
    o = Order(
        id=uuid.uuid4(), user_id=acct.user_id, broker_account_id=acct.id,
        instrument_type=InstrumentType.OPTION, symbol="SPY", side=side,
        order_type=OrderType.MARKET, quantity=Decimal(2), status=OrderStatus.FILLED,
        filled_quantity=Decimal(2), filled_avg_price=Decimal("1.50"),
        option_expiry=_EXPIRY, option_strike=Decimal("400"), option_right=OptionRight.PUT,
        created_at=_OPEN, closed_at=_OPEN,
    )
    db.add(o)
    db.flush()
    return o


def _total(res) -> Decimal:
    return sum((v[0] for v in res.values()), Decimal(0))


# ── capability sanity (fast, no DB) ──────────────────────────────────────────

def test_capabilities_gate_values():
    assert capabilities_for(BrokerName.ALPACA).authoritative_fill_history is True
    assert capabilities_for(BrokerName.WEBULL).authoritative_fill_history is False
    # A disconnected/unknown broker exposes nothing → never books expiries.
    assert capabilities_for(None).authoritative_fill_history is False


# ── A. Webull incomplete fills → NO synthetic expiry (the primary guard) ─────

def test_webull_incomplete_fills_no_synthetic_expiry():
    db = _session()
    acct = _acct(db, BrokerName.WEBULL)
    _buy_option_let_expire(db, acct)
    res = realized_pnl_by_day(
        db, acct.user_id, start=date(2026, 8, 1), end=date(2026, 9, 30),
        tz_name="America/New_York",
    )
    assert _total(res) == Decimal(0), (
        f"Webull must NOT synthesize an expiry loss from an open lot; got {_total(res)}"
    )


# ── B. Alpaca complete fills → expiry IS booked ──────────────────────────────

def test_alpaca_complete_fills_books_long_expiry():
    db = _session()
    acct = _acct(db, BrokerName.ALPACA)
    _buy_option_let_expire(db, acct)  # long 2 @ 1.50 → -1.50*2*100
    res = realized_pnl_by_day(
        db, acct.user_id, start=date(2026, 8, 1), end=date(2026, 9, 30),
        tz_name="America/New_York",
    )
    assert _total(res) == Decimal("-300"), (
        f"Alpaca long let-expire must book -300; got {_total(res)}"
    )


def test_alpaca_books_short_expiry_as_gain():
    db = _session()
    acct = _acct(db, BrokerName.ALPACA)
    _buy_option_let_expire(db, acct, side=OrderSide.SELL)  # short 2 @ 1.50 → +300
    res = realized_pnl_by_day(
        db, acct.user_id, start=date(2026, 8, 1), end=date(2026, 9, 30),
        tz_name="America/New_York",
    )
    assert _total(res) == Decimal("300"), (
        f"Alpaca short let-expire keeps premium +300; got {_total(res)}"
    )


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("ok", _name)
    print("all passed")
