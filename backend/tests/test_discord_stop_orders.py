"""A real STOP order resting at the broker, instead of a level we watch.

An emulated stop only exists while the poller runs — which is the wrong property
for the thing meant to protect a position when something goes wrong. These tests
pin the reconcile: what should rest, what does rest, and closing the gap.
"""
import os
import sys
import uuid
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_stop_orders as so
from app.models.order import OrderStatus


class _Guard:
    def __init__(self, stop=None, trail_qty=None, stop_order_id=None):
        self.user_id = uuid.uuid4()
        self.symbol = "MRVL"
        self.option_strike = Decimal("240")
        self.option_expiry = None
        self.stop_price = Decimal(stop) if stop else None
        self.trail_qty = Decimal(trail_qty) if trail_qty else None
        self.stop_order_id = stop_order_id


class _Order:
    def __init__(self, qty, stop, status=OrderStatus.ACCEPTED):
        self.id = uuid.uuid4()
        self.quantity = Decimal(qty)
        self.stop_price = Decimal(stop)
        self.status = status


@pytest.fixture
def db():
    class _DB:
        """Also answers the rejected-stop backoff lookup. `rejected` flips it on,
        so a test can exercise the back-off without building real Order rows."""
        def __init__(self): self.rows = {}; self.rejected = False
        def get(self, model, key): return self.rows.get(key)
        def add(self, o): self.rows[o.id] = o
        def execute(self, *a, **k):
            # The reconciler reads the broker's REASON for the last refusal, so
            # the fake has to answer with a row, not just a truthy id.
            hit = (self.rejected,) if self.rejected else None
            return type("R", (), {
                "one_or_none": lambda _s: hit,
                "scalar_one_or_none": lambda _s: hit and hit[0],
            })()
    return _DB()


@pytest.fixture
def broker():
    """Records what was placed and cancelled."""
    state = {"placed": [], "cancelled": []}

    def place(qty, price):
        o = _Order(qty, price)
        state["placed"].append((qty, price))
        state["last"] = o
        return o.id

    def cancel(order_id):
        state["cancelled"].append(order_id)

    state["place"], state["cancel"] = place, cancel
    return state


# ── sizing ───────────────────────────────────────────────────────────────────

def test_the_stop_covers_everything_held():
    assert so.desired_quantity(Decimal(4), _Guard(stop="2.00")) == Decimal(4)


def test_contracts_earmarked_for_a_trailing_exit_are_excluded():
    """A resting stop RESERVES what it covers. Covering the trailing slice too
    would make the trailing exit fail for insufficient quantity when it fires."""
    g = _Guard(stop="2.00", trail_qty=1)
    assert so.desired_quantity(Decimal(4), g) == Decimal(3)


def test_sizing_never_goes_negative():
    g = _Guard(stop="2.00", trail_qty=9)
    assert so.desired_quantity(Decimal(2), g) == Decimal(0)


# ── placing ──────────────────────────────────────────────────────────────────

def test_a_protected_position_with_no_stop_gets_one(db, broker):
    g = _Guard(stop="1.50")
    out = so.reconcile(db, g, Decimal(2), broker["place"], broker["cancel"])

    assert broker["placed"] == [(Decimal(2), Decimal("1.50"))]
    assert g.stop_order_id is not None
    assert "placed" in out


def test_an_already_correct_stop_is_left_alone(db, broker):
    order = _Order(2, "1.50"); db.add(order)
    g = _Guard(stop="1.50", stop_order_id=order.id)

    assert so.reconcile(db, g, Decimal(2), broker["place"], broker["cancel"]) == "in sync"
    assert broker["placed"] == [] and broker["cancelled"] == []


# ── keeping it in step with reality ──────────────────────────────────────────

def test_a_moved_level_replaces_the_resting_order(db, broker):
    """The 2nd trim lifts the stop to break-even — the resting order has to move
    with it, or it still protects at the old price."""
    order = _Order(2, "1.50"); db.add(order)
    g = _Guard(stop="2.30", stop_order_id=order.id)

    so.reconcile(db, g, Decimal(2), broker["place"], broker["cancel"])
    assert broker["cancelled"] == [order.id]
    assert broker["placed"] == [(Decimal(2), Decimal("2.30"))]


def test_a_changed_position_size_replaces_the_resting_order(db, broker):
    """After a trim fills, the stop covers more than is held — which the broker
    would reject when it fired."""
    order = _Order(4, "1.50"); db.add(order)
    g = _Guard(stop="1.50", stop_order_id=order.id)

    so.reconcile(db, g, Decimal(2), broker["place"], broker["cancel"])
    assert broker["placed"] == [(Decimal(2), Decimal("1.50"))]


def test_a_stop_that_already_filled_is_forgotten_not_reused(db, broker):
    order = _Order(2, "1.50", status=OrderStatus.FILLED); db.add(order)
    g = _Guard(stop="1.50", stop_order_id=order.id)

    so.reconcile(db, g, Decimal(2), broker["place"], broker["cancel"])
    assert broker["cancelled"] == []                 # nothing to cancel
    assert broker["placed"] == [(Decimal(2), Decimal("1.50"))]


def test_a_stop_cancelled_by_hand_at_the_broker_is_re_placed(db, broker):
    """Reconciling rather than placing once is what makes this self-heal."""
    order = _Order(2, "1.50", status=OrderStatus.CANCELED); db.add(order)
    g = _Guard(stop="1.50", stop_order_id=order.id)

    so.reconcile(db, g, Decimal(2), broker["place"], broker["cancel"])
    assert broker["placed"] == [(Decimal(2), Decimal("1.50"))]


# ── when there is nothing to protect ─────────────────────────────────────────

def test_a_vanished_position_has_its_stop_pulled(db, broker):
    order = _Order(2, "1.50"); db.add(order)
    g = _Guard(stop="1.50", stop_order_id=order.id)

    out = so.reconcile(db, g, Decimal(0), broker["place"], broker["cancel"])
    assert broker["cancelled"] == [order.id]
    assert g.stop_order_id is None
    assert "cancelled" in out


def test_no_level_means_no_order(db, broker):
    assert so.reconcile(db, _Guard(), Decimal(4), broker["place"], broker["cancel"]) == "idle"
    assert broker["placed"] == []


# ── making room for an exit ──────────────────────────────────────────────────

def test_release_frees_the_contracts_a_stop_reserves(db, broker):
    """A trim can't sell contracts a resting stop has reserved."""
    order = _Order(2, "1.50"); db.add(order)
    g = _Guard(stop="1.50", stop_order_id=order.id)

    assert so.release(db, g, broker["cancel"]) is True
    assert broker["cancelled"] == [order.id]
    assert g.stop_order_id is None


def test_release_is_a_no_op_with_nothing_resting(db, broker):
    assert so.release(db, _Guard(stop="1.50"), broker["cancel"]) is False


def test_a_failed_release_keeps_the_stop_rather_than_losing_track(db, broker):
    order = _Order(2, "1.50"); db.add(order)
    g = _Guard(stop="1.50", stop_order_id=order.id)

    def _boom(order_id):
        raise RuntimeError("broker unreachable")

    assert so.release(db, g, _boom) is False
    assert g.stop_order_id == order.id      # still tracked, retried next tick


def test_a_recent_rejection_backs_off_instead_of_retrying_every_tick(db, broker):
    """A rejected stop isn't resting, so without a back-off the reconciler places
    a fresh one every tick and collects the same rejection forever — which is
    exactly what happened: 29 identical rejections from one bad price."""
    db.rejected = True
    out = so.reconcile(db, _Guard(stop="1.50"), Decimal(2), broker["place"], broker["cancel"])

    assert broker["placed"] == []
    assert "backing off" in out


def _rejected_stop_at(db, user_id, when):
    from app.models.order import (
        InstrumentType, Order, OrderSide, OrderStatus, OrderType,
    )
    db.add(Order(
        id=uuid.uuid4(), user_id=user_id, symbol="NIO", side=OrderSide.SELL,
        order_type=OrderType.STOP, instrument_type=InstrumentType.OPTION,
        option_strike=Decimal("3.5"), quantity=Decimal(4),
        status=OrderStatus.REJECTED, created_at=when,
        reject_reason="HTTP Status: 417, Code: OPENAPI_STOP_PRICE_MUST_BE_LESS_THAN_MARKET_PRICE",
    ))
    db.commit()


def _guard_created(user_id, when):
    return type("G", (), {
        "user_id": user_id, "symbol": "NIO",
        "option_strike": Decimal("3.5"), "option_expiry": None,
        "created_at": when,
    })()


def test_a_rejection_from_a_previous_position_does_not_block_this_one():
    """The backoff window is per CONTRACT but a guard is per POSITION. A stop
    refused for a position that has since closed would otherwise leave the next
    entry on that contract unprotected for the rest of the window -- live, a
    fresh NIO entry went 15 minutes with no stop for exactly this reason."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    import app.services.discord_stop_orders as so
    from app.models.order import Order

    eng = create_engine("sqlite:///:memory:")
    Order.__table__.create(eng)
    user = uuid.uuid4()
    now = datetime.now(timezone.utc)

    with Session(eng) as db:
        _rejected_stop_at(db, user, now - timedelta(minutes=5))

        # A guard opened AFTER that rejection is a different position.
        assert so._recently_rejected(db, _guard_created(user, now - timedelta(minutes=1))) is False

        # One opened BEFORE it owns the rejection and must still back off.
        assert so._recently_rejected(db, _guard_created(user, now - timedelta(minutes=10))) is True


# ── a refused stop must not leave the position unprotected ───────────────────

# The REAL exception the placer raises. _place_trader_order marks the row
# REJECTED and then raises HTTPException(502, "broker_error: ..."), so str() of
# it begins "502: ". Testing with a hand-written RuntimeError message instead of
# this wrapper is exactly how a "502" entry in _TRANSIENT_MARKERS shipped and
# made the whole fallback dead code -- every broker rejection read as transient.
def _broker_refusal(detail):
    from fastapi import HTTPException
    return HTTPException(502, f"broker_error: {detail}")


WEBULL_BREACH = _broker_refusal(
    "HTTP Status: 417, Code: OPENAPI_STOP_PRICE_MUST_BE_LESS_THAN_MARKET_PRICE, "
    "Msg: Stop price must be less than market price for a sell order (0.21), "
    "RequestID: 184c0ee0-91fa-4b40-96a4-502429401503"
)

WEBULL_RATE_LIMIT = _broker_refusal(
    "HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: Too many requests"
)


class _StopDB:
    """Minimal session: resolves the resting stop order and records retirement."""

    def __init__(self, resting=None):
        self._resting = resting
        self.added = []

    def get(self, model, key):
        return self._resting if getattr(self._resting, "id", None) == key else None

    def execute(self, stmt):
        class _R:
            def one_or_none(self_inner): return None
            def scalar_one_or_none(self_inner): return None
        return _R()

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


def _stop_guard(**kw):
    from app.models.discord_position_guard import DiscordPositionGuard
    base = dict(symbol="NIO", option_strike=Decimal("3.5"), option_expiry=None,
                stop_price=Decimal("0.24"), stop_order_id=None, trail_qty=None,
                sell_count=2, user_id=uuid.uuid4())
    base.update(kw)
    return DiscordPositionGuard(**base)


def _refusing_placer(exc):
    def _place(qty, price):
        raise exc
    return _place


def test_a_refused_stop_closes_the_position(monkeypatch):
    """The refusal says the market is ALREADY through the level, so the stop we
    asked for would have fired the moment it rested. Closing now is what the
    stop was for."""
    import app.services.discord_stop_orders as so
    monkeypatch.setattr(so, "_recent_rejection_reason", lambda db, g: None)

    closed = []
    g = _stop_guard()
    out = so.reconcile(
        _StopDB(), g, Decimal(1),
        place_stop=_refusing_placer(WEBULL_BREACH),
        cancel_stop=lambda oid: None,
        close_position=closed.append,
    )
    assert closed == [Decimal(1)]
    assert "closed" in out
    assert g.closed_at is not None          # the ladder is done with it


def test_a_rate_limit_does_not_liquidate(monkeypatch):
    """A stop refused by a rate limit is not a stop the broker disagrees with.
    Closing a position over one would be a far worse bug than the one this
    fixes -- the backoff already handles it."""
    import app.services.discord_stop_orders as so
    monkeypatch.setattr(so, "_recent_rejection_reason", lambda db, g: None)

    from fastapi import HTTPException

    closed = []
    g = _stop_guard()
    try:
        so.reconcile(
            _StopDB(), g, Decimal(1),
            place_stop=_refusing_placer(WEBULL_RATE_LIMIT),
            cancel_stop=lambda oid: None,
            close_position=closed.append,
        )
    except HTTPException:
        pass
    else:
        raise AssertionError("a transient failure should propagate, not close")
    assert closed == []
    assert g.closed_at is None


def test_a_refusal_while_REPLACING_also_closes(monkeypatch):
    """The old stop is cancelled first, so a refusal here leaves the position
    barer than a failed first placement would."""
    from app.models.order import (
        InstrumentType, Order, OrderSide, OrderStatus, OrderType,
    )
    import app.services.discord_stop_orders as so
    monkeypatch.setattr(so, "_recent_rejection_reason", lambda db, g: None)

    resting = Order(
        id=uuid.uuid4(), user_id=uuid.uuid4(), symbol="NIO",
        side=OrderSide.SELL, order_type=OrderType.STOP,
        instrument_type=InstrumentType.OPTION, quantity=Decimal(2),
        stop_price=Decimal("0.15"), status=OrderStatus.SUBMITTED,
    )
    cancelled, closed = [], []
    g = _stop_guard(stop_order_id=resting.id)

    out = so.reconcile(
        _StopDB(resting), g, Decimal(1),
        place_stop=_refusing_placer(WEBULL_BREACH),
        cancel_stop=cancelled.append,
        close_position=closed.append,
    )
    assert cancelled == [resting.id]        # the old one went first
    assert closed == [Decimal(1)]
    assert "closed" in out


def test_without_a_close_callback_the_refusal_still_propagates(monkeypatch):
    """Callers that cannot close (no live position row) must not swallow it."""
    import app.services.discord_stop_orders as so
    monkeypatch.setattr(so, "_recent_rejection_reason", lambda db, g: None)
    from fastapi import HTTPException

    try:
        so.reconcile(
            _StopDB(), _stop_guard(), Decimal(1),
            place_stop=_refusing_placer(WEBULL_BREACH),
            cancel_stop=lambda oid: None,
        )
    except HTTPException:
        pass
    else:
        raise AssertionError("the refusal was swallowed")


def test_the_wrapper_status_code_is_not_read_as_a_transient_failure():
    """Regression: every broker rejection arrives as HTTPException(502, ...), so
    a bare "502" marker matched all of them and the close never fired. The
    RequestID here also embeds 502/429/401 to pin the numeric-substring trap."""
    import app.services.discord_stop_orders as so
    assert so._is_transient(str(WEBULL_BREACH)) is False


def test_a_brokers_own_code_name_still_reads_as_transient():
    """Webull writes TOO_MANY_REQUESTS, not "too many requests". The code name
    has to carry it ALONE -- Webull does not always append readable prose, and
    a rate limit misread as a refusal would liquidate the position."""
    import app.services.discord_stop_orders as so
    assert so._is_transient(str(WEBULL_RATE_LIMIT)) is True
    code_only = _broker_refusal("HTTP Status: 429, Code: TOO_MANY_REQUESTS")
    assert so._is_transient(str(code_only)) is True


def test_a_position_left_unprotected_by_an_earlier_refusal_is_rescued(db, broker):
    """The close must not depend on catching the exception as it happens.

    Closing only from the live exception works on the ONE tick that places the
    order. If anything goes wrong on that tick, the backoff then short-circuits
    every later tick -- reconcile returns "backing off" before place_stop is
    ever called -- and the position sits unprotected for the whole window. That
    is exactly what happened live: a stop was refused at 00:00:44, nothing
    closed, and every later tick answered "backing off (recent rejection)"
    while 1 contract stayed open with no stop behind it.
    """
    db.rejected = (
        "HTTP Status: 417, Code: OPENAPI_STOP_PRICE_MUST_BE_LESS_THAN_MARKET_PRICE, "
        "Msg: Stop price must be less than market price for a sell order (0.21)"
    )
    closed = []
    g = _Guard(stop="0.24")
    out = so.reconcile(
        db, g, Decimal(1), broker["place"], broker["cancel"],
        close_position=closed.append,
    )
    assert closed == [Decimal(1)], out
    assert broker["placed"] == []          # no pointless re-place first
    assert "closed" in out
    assert g.closed_at is not None


def test_a_transient_refusal_still_only_backs_off(db, broker):
    """Same path, but a rate limit must never liquidate."""
    db.rejected = "HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: Too many requests"
    closed = []
    out = so.reconcile(
        db, _Guard(stop="0.24"), Decimal(1), broker["place"], broker["cancel"],
        close_position=closed.append,
    )
    assert closed == []
    assert out == "backing off (recent rejection)"


def test_a_refusal_with_no_stored_reason_still_backs_off(db, broker):
    """An empty reject_reason must not read as "never rejected" -- that would
    send the reconciler back to placing a stop every single tick."""
    db.rejected = True          # truthy row, no text
    out = so.reconcile(
        db, _Guard(stop="0.24"), Decimal(1), broker["place"], broker["cancel"],
    )
    assert broker["placed"] == []
    assert out == "backing off (recent rejection)"
