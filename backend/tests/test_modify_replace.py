"""Regression tests for the modify (re-price) path.

Reproduces the prod incident (STKH, 28-Jul): the trader rapidly re-priced a SELL
to close a 595-share position. On Alpaca we did cancel-then-place, but Alpaca
hadn't released the shares the cancelled order reserved, so the immediate
re-place was rejected ("insufficient qty available … held_for_orders: 595") — and
because the old order was already cancelled, the subscriber was left with NO sell
order. Every rapid re-price hit the same race, so subscribers stopped receiving
his sell.

The fixes exercised here (``_modify_place_one``):
  #2 native in-place replace — brokers that support it (Alpaca) re-price
     atomically, no cancel and no share-release race at all;
  #1 retry-with-backoff — the cancel+place FALLBACK (SnapTrade / IBKR) retries
     the place through the broker's share-release lag instead of losing the order.

Pure over its args (no DB), so we drive it with fake adapters. Backoff sleeps are
neutered so the test is instant. Runs standalone or under pytest.
"""
import os
import sys
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.copy_engine as ce
from app.brokers import BrokerOrderRequest, BrokerOrderResult
from app.models.order import InstrumentType, OrderSide, OrderStatus, OrderType

ce._MODIFY_PLACE_BACKOFF_S = 0  # don't actually sleep in the retry loop


class _OldCh:
    """Stand-in for the old mirror Order row — _modify_place_one only reads .id
    and .broker_order_id off it."""
    def __init__(self):
        self.id = uuid.uuid4()
        self.broker_order_id = "old-broker-id"


def _result(bkr_id="new-broker-id"):
    return BrokerOrderResult(
        broker_order_id=bkr_id,
        status=OrderStatus.SUBMITTED,
        submitted_at=None,  # not read by the modify path
        filled_quantity=Decimal("0"),
    )


class _Conflict(Exception):
    """Alpaca's share-release rejection right after a cancel."""
    def __str__(self):
        return ('{"code":40310000,"message":"insufficient qty available for order '
                '(requested: 595, available: 0)","held_for_orders":"595"}')


def _req():
    return BrokerOrderRequest(
        instrument_type=InstrumentType.STOCK,
        symbol="STKH",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=Decimal("595"),
        limit_price=Decimal("7.00"),
        is_closing=True,
        client_order_id=str(uuid.uuid4()),
    )


def _run(adapter):
    old = _OldCh()
    new_id = uuid.uuid4()
    return ce._modify_place_one((old, adapter, _req(), new_id))


# ── #2 native in-place replace ────────────────────────────────────────────────

class _ReplaceAdapter:
    supports_replace = True
    def __init__(self, fail=False):
        self.fail = fail
        self.replaced = False
        self.cancelled = False
        self.placed = False
    def replace_order(self, broker_order_id, req):
        self.replaced = True
        if self.fail:
            raise RuntimeError("422 order not replaceable")
        return _result()
    def cancel_order(self, broker_order_id):
        self.cancelled = True
        return True
    def place_order(self, req):
        self.placed = True
        return _result()


def test_replace_used_when_supported_no_cancel():
    """Alpaca: re-price goes through the atomic replace — never cancels/places."""
    ad = _ReplaceAdapter()
    _old, _new, resp, err = _run(ad)
    assert ad.replaced and not ad.cancelled and not ad.placed
    assert resp is not None and err is None


def test_replace_failure_keeps_old_order():
    """Atomic replace failed → old order left intact (never cancelled), reported
    as replace_failed so Phase 3 keeps the old mirror instead of losing it."""
    ad = _ReplaceAdapter(fail=True)
    _old, _new, resp, err = _run(ad)
    assert ad.replaced and not ad.cancelled and not ad.placed
    assert resp is None
    assert err is not None and err.startswith("replace_failed")


# ── #1 cancel+place fallback with share-release retry ─────────────────────────

class _CancelPlaceAdapter:
    """No in-place replace (SnapTrade/IBKR shape). place_order raises a conflict
    ``fail_first`` times to simulate the share-release lag, then succeeds."""
    supports_replace = False
    def __init__(self, fail_first=0, cancel_result=True):
        self.fail_first = fail_first
        self.cancel_result = cancel_result
        self.place_calls = 0
        self.cancelled = False
    def cancel_order(self, broker_order_id):
        self.cancelled = True
        return self.cancel_result
    def place_order(self, req):
        self.place_calls += 1
        if self.place_calls <= self.fail_first:
            raise _Conflict()
        return _result()


def test_fallback_retries_through_share_release_race():
    """The STKH bug: first place bounces on 'insufficient qty', the retry (after
    the broker releases the shares) succeeds → subscriber keeps the sell."""
    ad = _CancelPlaceAdapter(fail_first=2)
    _old, _new, resp, err = _run(ad)
    assert ad.cancelled
    assert ad.place_calls == 3            # 2 conflicts + 1 success
    assert resp is not None and err is None


def test_fallback_gives_up_after_budget_and_reports_place_failed():
    """If the race never clears within the retry budget, report place_failed
    (the old order was already cancelled) rather than hang forever."""
    ad = _CancelPlaceAdapter(fail_first=99)
    _old, _new, resp, err = _run(ad)
    assert ad.place_calls == ce._MODIFY_PLACE_ATTEMPTS
    assert resp is None and err.startswith("place_failed")


def test_fallback_bails_when_cancel_is_noop():
    """cancel returned False (order already terminal / likely filled) → never
    place a replacement (would double the position)."""
    ad = _CancelPlaceAdapter(cancel_result=False)
    _old, _new, resp, err = _run(ad)
    assert ad.place_calls == 0
    assert resp is None and err == "cancel_noop_already_terminal"


def test_fallback_non_conflict_error_does_not_retry():
    """A non-conflict place error (e.g. bad price) fails once, no retry storm."""
    class _BadPrice(_CancelPlaceAdapter):
        def place_order(self, req):
            self.place_calls += 1
            raise RuntimeError("limit price is invalid")
    ad = _BadPrice()
    _old, _new, resp, err = _run(ad)
    assert ad.place_calls == 1            # tried once, gave up (not a conflict)
    assert resp is None and err.startswith("place_failed")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll modify/replace tests passed.")
