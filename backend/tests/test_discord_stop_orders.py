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
            hit = uuid.uuid4() if self.rejected else None
            return type("R", (), {"scalar_one_or_none": lambda _s: hit})()
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
