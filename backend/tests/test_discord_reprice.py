"""One repriced attempt for a Discord entry that didn't fill.

The rules under test are the ones that bound what this can cost: the retry is
priced off the ORIGINAL limit (not the running ask), it happens exactly ONCE,
and it never spends past the trader's max-per-contract ceiling.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_reprice as rp
from app.models.order import InstrumentType, OrderStatus, OrderType


class _Settings:
    def __init__(self, pct="10", cap=None, after=30, order_cap=None):
        self.discord_reprice_pct = Decimal(pct)
        self.discord_reprice_after_seconds = after
        self.discord_max_per_contract = Decimal(str(cap)) if cap is not None else None
        self.discord_max_per_order = (
            Decimal(str(order_cap)) if order_cap is not None else None
        )


class _Order:
    def __init__(self, limit="2.00", status=OrderStatus.ACCEPTED, repriced=None,
                 instrument=InstrumentType.OPTION):
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.broker_account_id = uuid.uuid4()
        self.broker_order_id = "brk-1"
        self.symbol = "MSFT"
        self.limit_price = Decimal(limit)
        self.quantity = Decimal(2)
        self.status = status
        self.discord_repriced_at = repriced
        self.instrument_type = instrument
        self.side = "buy"
        self.order_type = OrderType.LIMIT
        self.option_expiry = self.option_strike = self.option_right = None


class _Adapter:
    supports_replace = True

    def __init__(self, raises=False):
        self.replaced = []
        self.cancelled = []
        self.reqs = []
        self._raises = raises

    def replace_order(self, broker_order_id, req):
        if self._raises:
            raise RuntimeError("broker refused")
        self.replaced.append(req.limit_price)
        self.reqs.append(req)
        return type("R", (), {"broker_order_id": "brk-2"})()

    def cancel_order(self, broker_order_id):
        self.cancelled.append(broker_order_id)

    def place_order(self, req):
        self.replaced.append(req.limit_price)
        return type("R", (), {"broker_order_id": "brk-3"})()


@pytest.fixture
def wired(monkeypatch):
    """Point reprice_one at fakes, and hand back what it did."""
    state = {}

    def _setup(order, settings, adapter=None):
        adapter = adapter or _Adapter()
        state.update(order=order, adapter=adapter, committed=0)

        class _DB:
            def get(self, model, key):
                name = getattr(model, "__name__", "")
                if name == "Order":
                    return order
                if name == "TraderSettings":
                    return settings
                return type("A", (), {"encrypted_credentials": "x", "broker": "WEBULL"})()
            def commit(self): state["committed"] += 1
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(rp, "SessionLocal", lambda: _DB())
        monkeypatch.setattr("app.brokers.adapter_for", lambda a, c: adapter)
        monkeypatch.setattr("app.services.crypto.decrypt_json", lambda c: {})
        return state
    return _setup


# ── the price ────────────────────────────────────────────────────────────────

def test_the_retry_is_priced_off_the_original_limit(wired):
    """Not off the current ask. Chasing the ask is unbounded — a contract that
    ran 300% would be bought at 300%."""
    s = wired(_Order(limit="2.00"), _Settings(pct="10"))
    assert rp.reprice_one(s["order"].id) == "repriced to 2.20"
    assert s["adapter"].replaced == [Decimal("2.20")]
    assert s["order"].limit_price == Decimal("2.20")


def test_the_percentage_is_configurable(wired):
    s = wired(_Order(limit="2.00"), _Settings(pct="25"))
    rp.reprice_one(s["order"].id)
    assert s["adapter"].replaced == [Decimal("2.50")]


def test_the_new_price_is_rounded_to_a_cent(wired):
    s = wired(_Order(limit="1.33"), _Settings(pct="10"))
    rp.reprice_one(s["order"].id)
    assert s["adapter"].replaced == [Decimal("1.46")]


# ── exactly once ─────────────────────────────────────────────────────────────

def test_an_order_is_repriced_only_once(wired):
    s = wired(_Order(limit="2.00"), _Settings())
    rp.reprice_one(s["order"].id)
    assert s["order"].discord_repriced_at is not None

    # A second tick must find it already stamped and leave it alone.
    assert rp.reprice_one(s["order"].id) == "already"
    assert len(s["adapter"].replaced) == 1


def test_an_order_that_filled_between_scan_and_act_is_left_alone(wired):
    """The common case on a busy contract — it must not be repriced after the
    fill, which would buy a second position."""
    s = wired(_Order(status=OrderStatus.FILLED), _Settings())
    assert rp.reprice_one(s["order"].id) == "gone"
    assert s["adapter"].replaced == []


def test_the_stamp_lands_before_the_broker_call(wired):
    """If the broker call throws after the order was actually accepted, a later
    tick must not reprice it again. One missed retry beats an unbounded chase."""
    s = wired(_Order(limit="2.00"), _Settings(), _Adapter(raises=True))
    assert rp.reprice_one(s["order"].id) == "reprice failed"
    assert s["order"].discord_repriced_at is not None
    assert s["order"].limit_price == Decimal("2.00")     # unchanged


# ── the ceiling still applies ────────────────────────────────────────────────

def test_a_retry_that_breaches_the_ceiling_cancels_instead(wired):
    """Getting filled must not cost more than the trader said a contract is
    worth — the ceiling is not overridden by the mechanism meant to get us in."""
    s = wired(_Order(limit="5.00"), _Settings(pct="10", cap=500))   # 5.50 -> $550
    assert rp.reprice_one(s["order"].id) == "cancelled (ceiling)"
    assert s["adapter"].replaced == []
    assert s["adapter"].cancelled == ["brk-1"]
    assert s["order"].status is OrderStatus.CANCELED


def test_a_retry_inside_the_ceiling_goes_through(wired):
    s = wired(_Order(limit="4.00"), _Settings(pct="10", cap=500))   # 4.40 -> $440
    assert rp.reprice_one(s["order"].id).startswith("repriced")


def test_no_ceiling_means_no_check(wired):
    s = wired(_Order(limit="50.00"), _Settings(pct="10", cap=None))
    assert rp.reprice_one(s["order"].id).startswith("repriced")


def test_the_ceiling_is_per_contract_not_per_order():
    """Mirrors the entry-side rule: the test is on ONE contract's premium x 100,
    never the order's total."""
    ts = _Settings(cap=500)
    assert rp._breaches_ceiling(Decimal("4.00"), ts, is_option=True) is False   # $400
    assert rp._breaches_ceiling(Decimal("6.00"), ts, is_option=True) is True    # $600
    # A stock is priced per share, so no x100.
    assert rp._breaches_ceiling(Decimal("6.00"), ts, is_option=False) is False


# ── falling back when the broker can't replace ───────────────────────────────

def test_a_broker_without_atomic_replace_is_left_alone(wired):
    """Cancel-then-place is not a safe fallback. On Webull a 4-lot buy was
    cancelled 30s after placement, the re-place never landed, and the position
    the trader believed they held did not exist — every later trim then fired
    into nothing. A resting unfilled limit is recoverable; a vanished entry is
    not."""
    adapter = _Adapter()
    adapter.supports_replace = False
    s = wired(_Order(limit="2.00"), _Settings(), adapter)

    out = rp.reprice_one(s["order"].id)
    assert "skipped" in out
    assert adapter.cancelled == []                 # nothing pulled
    assert adapter.replaced == []                  # nothing placed
    assert s["order"].limit_price == Decimal("2.00")
    assert s["order"].discord_repriced_at is not None   # not retried in a loop


def test_replacing_tracks_the_new_broker_id(wired):
    s = wired(_Order(limit="2.00"), _Settings())
    rp.reprice_one(s["order"].id)
    assert s["order"].broker_order_id == "brk-2"


def test_a_broker_with_atomic_replace_still_reprices(wired):
    """Alpaca can replace in one step, so the feature keeps working there —
    the guard is about HOW the price is moved, not about disabling the retry."""
    adapter = _Adapter()
    adapter.supports_replace = True
    s = wired(_Order(limit="2.00"), _Settings(), adapter)

    assert rp.reprice_one(s["order"].id) == "repriced to 2.20"
    assert adapter.replaced == [Decimal("2.20")]
    assert adapter.cancelled == []              # replaced, never cancelled


def test_a_skipped_broker_is_not_retried_every_tick(wired):
    """Stamping on the skip keeps the scanner from re-examining the same order
    forever — it is a decision, not a transient failure."""
    adapter = _Adapter()
    adapter.supports_replace = False
    s = wired(_Order(limit="2.00"), _Settings(), adapter)

    rp.reprice_one(s["order"].id)
    assert rp.reprice_one(s["order"].id) == "already"


# ── the replacement's identity ───────────────────────────────────────────────

def test_the_replacement_gets_its_own_client_order_id(wired, monkeypatch):
    """Alpaca's replace opens a NEW order, and a client_order_id can only be
    held by one ACTIVE order. Reusing the id the original is still resting
    under is answered with 422 "client_order_id must be unique" — the reprice
    is stamped as attempted and silently never lands."""
    monkeypatch.setattr(
        "app.services.order_intent.mark_app_originated", lambda oid: None
    )
    st = wired(_Order(limit="2.00"), _Settings())
    rp.reprice_one(st["order"].id)

    sent = st["adapter"].reqs[0].client_order_id
    assert sent != str(st["order"].id)
    uuid.UUID(sent)   # the listener parses it as a UUID


def test_the_replacement_is_marked_app_originated_before_the_call(wired, monkeypatch):
    """The broker echoes client_order_id back on its order stream. Without the
    marker the listener reads the replacement as an EXTERNAL trade and inserts
    a duplicate parent plus a second fanout — the doubling bug."""
    marked: list = []
    monkeypatch.setattr(
        "app.services.order_intent.mark_app_originated",
        lambda oid: marked.append((oid, len(st["adapter"].reqs))),
    )
    st = wired(_Order(limit="2.00"), _Settings())
    rp.reprice_one(st["order"].id)

    assert len(marked) == 1
    marked_id, replaces_so_far = marked[0]
    assert replaces_so_far == 0          # marked BEFORE the broker call
    assert str(marked_id) == st["adapter"].reqs[0].client_order_id


# ── the retry must not spend past the ORDER ceiling either ───────────────────

def test_a_retry_that_breaches_the_order_ceiling_cancels(wired):
    """The reprice raises the price, which lifts what ONE contract costs and
    what the WHOLE order costs together. Checking only the per-contract cap
    would let a large order slip past max_per_order on the retry after being
    refused on the way in — the mechanism meant to get the trader IN quietly
    overriding what they said they would put in."""
    order = _Order(limit="2.00")          # qty 2 → 2.20 x 100 x 2 = $440 after +10%
    st = wired(order, _Settings(order_cap="400"))
    out = rp.reprice_one(order.id)

    assert "cancelled" in out
    assert st["adapter"].cancelled == ["brk-1"]
    assert st["adapter"].replaced == []


def test_a_retry_inside_the_order_ceiling_goes_through(wired):
    order = _Order(limit="2.00")
    st = wired(order, _Settings(order_cap="500"))
    out = rp.reprice_one(order.id)

    assert "repriced" in out
    assert st["adapter"].replaced == [Decimal("2.20")]


def test_the_two_reprice_ceilings_are_independent(wired):
    """A contract well inside the per-contract cap can still make an order that
    is over the order cap."""
    order = _Order(limit="2.00")
    st = wired(order, _Settings(cap="1000", order_cap="400"))
    assert "cancelled" in rp.reprice_one(order.id)
    assert st["adapter"].replaced == []
