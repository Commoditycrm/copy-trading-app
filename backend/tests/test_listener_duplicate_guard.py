"""A listener must not re-create an order our own app just placed.

THE BUG
-------
Every listener dedupes with ``WHERE broker_order_id = <the id the feed gave>``.
But for an order WE placed, the id we stored and the id the feed reports are
often not the same value:

  * Webull  — WebullAdapter.place_order stores OUR client_order_id as
    broker_order_id, while webull_listener looks the order up by WEBULL's
    order_id. Two identifiers, one order: the lookup can never match.
  * SnapTrade — files an order under a DIFFERENT id than it returned at
    placement (see _relink_orphaned_mirror_ids, "prod: 49 stuck mirrors").

So the SELECT misses, the listener treats it as an externally-placed trade, and
inserts a SECOND parent row — which is then fanned out again. Prod, 2026-09-21:
8 duplicate pairs, 4 of which fanned out, so those subscribers received TWO
mirror orders for ONE trader trade.

The duplicate also loses the contract. The listener rebuilds the order from the
feed payload, and when that payload doesn't identify it as an option the row is
typed STOCK with strike/expiry/right NULL. realized_pnl_by_order then applies
unit=1 instead of 100 — an $86 trade shown as $0.86 — and because
_instrument_key returns ("STK","SPY") vs ("OPT","SPY",…) the two rows sit in
different FIFO buckets and may never close against each other at all.

trade_listener has guarded this since the Alpaca doubling bug. It was never
carried across to the other three.

TWO MECHANISMS, because the brokers differ
------------------------------------------
Webull and IBKR echo our client_order_id, so they can use the existing Redis
marker directly. SnapTrade has NO client-order-id concept at all, so it matches
on our own recent app-placed orders instead and ADOPTS the feed's id onto the
row we already have — adopting rather than skipping, because a trader really
does place orders outside the app and those must still be mirrored.
"""
import os
import sys
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import order_intent

_TRADER = uuid.UUID("b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e")


class _FakeRedis:
    """In-memory stand-in with the two operations order_intent uses."""
    def __init__(self):
        self.store: dict[str, str] = {}

    def setex(self, k, _ttl, v):
        self.store[k] = v

    def get(self, k):
        return self.store.get(k)

    def getdel(self, k):
        return self.store.pop(k, None)


@pytest.fixture(autouse=True)
def _redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(order_intent, "get_sync_redis", lambda: r)
    return r


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    for m in (User, BrokerAccount, Order):
        m.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    db.add(User(id=_TRADER, email="t@example.com", password_hash="x",
                role=UserRole.TRADER, is_active=True))
    db.commit()
    return db


def _order(db, **kw):
    o = Order(
        id=kw.pop("id", uuid.uuid4()), user_id=_TRADER,
        instrument_type=kw.pop("instrument_type", InstrumentType.OPTION),
        symbol=kw.pop("symbol", "SPY"),
        option_expiry=kw.pop("option_expiry", None),
        option_strike=kw.pop("option_strike", None),
        option_right=kw.pop("option_right", None),
        side=kw.pop("side", OrderSide.BUY),
        order_type=OrderType.LIMIT,
        quantity=kw.pop("quantity", Decimal("3")),
        status=kw.pop("status", OrderStatus.SUBMITTED),
        broker_order_id=kw.pop("broker_order_id", "our-32-hex-id"),
        **kw,
    )
    db.add(o)
    db.commit()
    return o


# ── the marker itself ───────────────────────────────────────────────────────
def test_marker_round_trip():
    oid = uuid.uuid4()
    assert order_intent.is_app_originated(oid) is False
    order_intent.mark_app_originated(oid)
    assert order_intent.is_app_originated(oid) is True


def test_consume_is_single_use():
    """Two feed rows must never both claim the same order — the second would
    hide a genuine second trade behind the first."""
    oid = uuid.uuid4()
    order_intent.mark_app_originated(oid)
    assert order_intent.consume_app_originated(oid) is True
    assert order_intent.consume_app_originated(oid) is False


def test_marker_failures_fail_open(monkeypatch):
    """Redis down must never drop a real external order — the worst case is the
    pre-fix duplicate, not a missed mirror."""
    class _Broken:
        def get(self, k): raise RuntimeError("redis down")
        def getdel(self, k): raise RuntimeError("redis down")
        def setex(self, k, t, v): raise RuntimeError("redis down")
    monkeypatch.setattr(order_intent, "get_sync_redis", lambda: _Broken())
    oid = uuid.uuid4()
    order_intent.mark_app_originated(oid)          # must not raise
    assert order_intent.is_app_originated(oid) is False
    assert order_intent.consume_app_originated(oid) is False


# ── the SnapTrade adopt path ────────────────────────────────────────────────
def test_adopts_our_order_instead_of_duplicating():
    """The core fix for SnapTrade: the feed's id is attached to the row we
    already have, rather than a second parent being created."""
    db = _db()
    ours = _order(db, broker_order_id="OUR-PLACE-ID")
    order_intent.mark_app_originated(ours.id)

    got = order_intent.adopt_app_placed_order(
        db, _TRADER, "FEED-SIDE-ID", symbol="SPY", side=OrderSide.BUY,
        quantity=Decimal("3"), instrument_type=InstrumentType.OPTION,
    )
    assert got is not None and got.id == ours.id
    assert got.broker_order_id == "FEED-SIDE-ID", "the feed id is adopted onto our row"


def test_adoption_preserves_the_option_contract():
    """Why adopting beats inserting: our row knows the strike and expiry because
    WE chose them. A row rebuilt from the feed can come back typed STOCK, which
    drops the x100 multiplier from realized P&L."""
    from datetime import date
    db = _db()
    ours = _order(db, broker_order_id="OUR-PLACE-ID",
                  option_expiry=date(2026, 9, 21), option_strike=Decimal("771"))
    order_intent.mark_app_originated(ours.id)
    got = order_intent.adopt_app_placed_order(
        db, _TRADER, "FEED-SIDE-ID", symbol="SPY", side=OrderSide.BUY,
        quantity=Decimal("3"), instrument_type=InstrumentType.OPTION,
    )
    assert got.instrument_type == InstrumentType.OPTION
    assert got.option_strike == Decimal("771")
    assert got.option_expiry == date(2026, 9, 21)


def test_a_genuine_external_order_is_never_adopted():
    """A trader really does trade outside the app. With no marker set, this must
    return None so the caller inserts and mirrors it normally."""
    db = _db()
    _order(db, broker_order_id="OUR-PLACE-ID")     # exists, but NOT marked
    got = order_intent.adopt_app_placed_order(
        db, _TRADER, "FEED-SIDE-ID", symbol="SPY", side=OrderSide.BUY,
        quantity=Decimal("3"), instrument_type=InstrumentType.OPTION,
    )
    assert got is None


def test_adoption_will_not_claim_a_different_trade():
    """Tight matching. A marked order for a DIFFERENT contract, side or size is
    not ours to claim — mis-adopting would rewrite one trade as another."""
    db = _db()
    for kw in (
        {"symbol": "QQQ"},
        {"side": OrderSide.SELL},
        {"quantity": Decimal("5")},
        {"instrument_type": InstrumentType.STOCK},
    ):
        o = _order(db, broker_order_id=f"id-{kw}", **kw)
        order_intent.mark_app_originated(o.id)
    got = order_intent.adopt_app_placed_order(
        db, _TRADER, "FEED-SIDE-ID", symbol="SPY", side=OrderSide.BUY,
        quantity=Decimal("3"), instrument_type=InstrumentType.OPTION,
    )
    assert got is None, "no candidate matches all of symbol/side/qty/instrument"


def test_only_one_feed_row_can_adopt_an_order():
    """Single-use. Otherwise a genuine second trade would be swallowed by the
    first one's marker."""
    db = _db()
    ours = _order(db, broker_order_id="OUR-PLACE-ID")
    order_intent.mark_app_originated(ours.id)
    first = order_intent.adopt_app_placed_order(
        db, _TRADER, "FEED-ID-1", symbol="SPY", side=OrderSide.BUY,
        quantity=Decimal("3"), instrument_type=InstrumentType.OPTION,
    )
    second = order_intent.adopt_app_placed_order(
        db, _TRADER, "FEED-ID-2", symbol="SPY", side=OrderSide.BUY,
        quantity=Decimal("3"), instrument_type=InstrumentType.OPTION,
    )
    assert first is not None
    assert second is None, "a second feed row must not claim the same order"


def test_mirrors_are_never_adopted():
    """Only a trader's OWN parent orders participate. A subscriber mirror has a
    parent_order_id and must be left alone."""
    db = _db()
    m = _order(db, broker_order_id="MIRROR", parent_order_id=uuid.uuid4())
    order_intent.mark_app_originated(m.id)
    got = order_intent.adopt_app_placed_order(
        db, _TRADER, "FEED-SIDE-ID", symbol="SPY", side=OrderSide.BUY,
        quantity=Decimal("3"), instrument_type=InstrumentType.OPTION,
    )
    assert got is None


# ── the guard is actually wired into every listener ─────────────────────────
def test_every_listener_guards_against_app_placed_orders():
    """The regression itself: trade_listener had this and the other three did
    not, which is why one trader's account produced 8 duplicate pairs."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "services"
    # Match the qualified CALL, not a bare name — a bare substring still
    # matches a renamed/disabled identifier, so it would not catch a regression.
    for name, needle in (
        ("trade_listener", "order_intent.is_app_originated("),
        ("webull_listener", "order_intent.is_app_originated("),
        ("ibkr_listener", "order_intent.is_app_originated("),
        # SnapTrade has no client-order-id, so it uses the adopt path instead.
        ("snaptrade_listener", "order_intent.adopt_app_placed_order("),
    ):
        src = (root / f"{name}.py").read_text()
        assert needle in src, f"{name} has no duplicate guard — expected {needle}"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"run under pytest for fixtures; {len(fns)} tests defined")
