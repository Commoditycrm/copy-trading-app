"""Regression guard for SnapTrade subscriber mirror id re-linking.

SnapTrade returns one order id at place time and lists the same order under a
DIFFERENT id in its orders feed, so a mirror can carry a broker_order_id that
never appears in the feed — its fill is then never matched and it stays
SUBMITTED forever (prod: 49 stuck mirrors). `_relink_orphaned_mirror_ids` adopts
the feed id onto such an orphaned mirror, but ONLY when the (contract, side)
match is unambiguous. These tests lock both behaviours in.

Real in-memory SQLite (shared across sessions via StaticPool). No broker/network.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.brokers.snaptrade as snap
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.services import snaptrade_listener

_SUB = uuid.uuid4()
_ACCT = uuid.uuid4()
_T = datetime(2026, 9, 17, 17, 46, tzinfo=timezone.utc)


def _make_sessionmaker():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Order.__table__.create(eng)
    return sessionmaker(bind=eng)


def _mirror(db, *, boid, symbol="MU", side=OrderSide.BUY, status=OrderStatus.SUBMITTED):
    o = Order(
        id=uuid.uuid4(),
        user_id=_SUB,
        broker_account_id=_ACCT,
        parent_order_id=uuid.uuid4(),  # a mirror
        instrument_type=InstrumentType.STOCK,
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        status=status,
        broker_order_id=boid,
        created_at=_T,
    )
    db.add(o)
    db.flush()
    return o


def _feed(boid, symbol="MU", action="BUY"):
    return {"brokerage_order_id": boid, "action": action, "symbol": symbol}


def _patch(monkeypatch, Session):
    monkeypatch.setattr(snaptrade_listener, "SessionLocal", Session)
    # Parse straight from our simple feed dict — avoid depending on SnapTrade's shape.
    monkeypatch.setattr(snap, "parse_snaptrade_order_symbol", lambda o: {
        "symbol": o["symbol"], "instrument_type": InstrumentType.STOCK,
        "option_expiry": None, "option_strike": None, "option_right": None,
    })


def test_orphaned_mirror_adopts_feed_id(monkeypatch):
    Session = _make_sessionmaker()
    _patch(monkeypatch, Session)
    with Session() as db:
        m = _mirror(db, boid="TRADE-UUID-1")  # place-time id, absent from feed
        db.commit()
        mid = m.id

    snaptrade_listener._relink_orphaned_mirror_ids(_SUB, _ACCT, [_feed("FEED-ID-1")])

    with Session() as db:
        assert db.get(Order, mid).broker_order_id == "FEED-ID-1"


def test_ambiguous_contract_side_is_left_alone(monkeypatch):
    Session = _make_sessionmaker()
    _patch(monkeypatch, Session)
    with Session() as db:
        a = _mirror(db, boid="TRADE-A")
        b = _mirror(db, boid="TRADE-B")  # two orphans, same contract+side → ambiguous
        db.commit()
        aid, bid = a.id, b.id

    snaptrade_listener._relink_orphaned_mirror_ids(_SUB, _ACCT, [_feed("FEED-X")])

    with Session() as db:
        assert db.get(Order, aid).broker_order_id == "TRADE-A"
        assert db.get(Order, bid).broker_order_id == "TRADE-B"


def test_already_matching_id_is_untouched(monkeypatch):
    Session = _make_sessionmaker()
    _patch(monkeypatch, Session)
    with Session() as db:
        m = _mirror(db, boid="FEED-OK")  # id already in the feed → not orphaned
        db.commit()
        mid = m.id

    snaptrade_listener._relink_orphaned_mirror_ids(_SUB, _ACCT, [_feed("FEED-OK")])

    with Session() as db:
        assert db.get(Order, mid).broker_order_id == "FEED-OK"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
