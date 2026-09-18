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
    """Stand-in for the old mirror Order row.

    _modify_place_one reads only .id and .broker_order_id. The force-fill path
    ALSO reads .quantity and .filled_quantity — that is its double-buy guard,
    which sizes the replacement to the true unfilled remainder. This stub fell
    behind that change and every force-fill test blew up on the missing
    attribute, so the guard has been running untested.
    """
    def __init__(self, quantity="5", filled_quantity="0"):
        self.id = uuid.uuid4()
        self.broker_order_id = "old-broker-id"
        self.quantity = Decimal(quantity)
        self.filled_quantity = Decimal(filled_quantity)


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
    ``fail_first`` times to simulate the share-release lag, then succeeds.

    ``final_filled`` is what a post-cancel get_order reports the order ended up
    filling. The force-fill path re-reads that after cancelling, because a
    cancel being ACCEPTED does not mean zero filled — an async cancel can race a
    fill, and placing the full size on top would double the subscriber.
    """
    supports_replace = False
    def __init__(self, fail_first=0, cancel_result=True, final_filled=None):
        self.fail_first = fail_first
        self.cancel_result = cancel_result
        self.final_filled = final_filled
        self.place_calls = 0
        self.placed_quantities = []
        self.cancelled = False
    def cancel_order(self, broker_order_id):
        self.cancelled = True
        return self.cancel_result
    def get_order(self, broker_order_id):
        if self.final_filled is None:
            raise RuntimeError("no post-cancel read available")
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status=OrderStatus.CANCELED,
            submitted_at=None,
            filled_quantity=Decimal(self.final_filled),
        )
    def place_order(self, req):
        self.place_calls += 1
        self.placed_quantities.append(req.quantity)
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


# ── #3 replace-chain-pending retry (Alpaca 42210000) ──────────────────────────
# Prod RDGT 2026-08-10: a close re-priced 3s after the previous re-price hit
# Alpaca's "order chain not fully replaced" (42210000) — the prior replacement
# was still settling. With no retry the subscriber kept a STALE sell that never
# filled while the trader had already exited. The replace path now retries
# through the transient chain error before falling back to keep-old.

class _ChainPending(Exception):
    """Alpaca's transient 'previous replacement still settling' rejection."""
    def __str__(self):
        return '{"code":42210000,"message":"order chain not fully replaced"}'


class _ReplaceChainAdapter:
    """Atomic-replace broker whose replace_order raises the chain-pending error
    ``fail_first`` times (chain settling), then succeeds. Never cancels/places."""
    supports_replace = True
    def __init__(self, fail_first=0):
        self.fail_first = fail_first
        self.replace_calls = 0
        self.cancelled = False
        self.placed = False
    def replace_order(self, broker_order_id, req):
        self.replace_calls += 1
        if self.replace_calls <= self.fail_first:
            raise _ChainPending()
        return _result()
    def cancel_order(self, broker_order_id):
        self.cancelled = True
        return True
    def place_order(self, req):
        self.placed = True
        return _result()


def test_replace_retries_through_chain_pending():
    """The RDGT bug: replace bounces on 'order chain not fully replaced', the
    retry (after the chain settles) succeeds → subscriber lands on the new close.
    Never falls back to cancel/place."""
    ad = _ReplaceChainAdapter(fail_first=2)
    _old, _new, resp, err = _run(ad)
    assert ad.replace_calls == 3          # 2 chain-pending + 1 success
    assert not ad.cancelled and not ad.placed
    assert resp is not None and err is None


def test_replace_chain_pending_gives_up_keeps_old():
    """If the chain never settles within budget, report replace_failed so Phase 3
    keeps the old order (never cancelled) — no worse than before the retry."""
    ad = _ReplaceChainAdapter(fail_first=99)
    _old, _new, resp, err = _run(ad)
    assert ad.replace_calls == ce._MODIFY_PLACE_ATTEMPTS
    assert not ad.cancelled and not ad.placed
    assert resp is None and err.startswith("replace_failed")


def test_replace_non_chain_error_does_not_retry():
    """A non-chain replace error (e.g. not replaceable) fails once, no retry
    storm, and keeps the old order."""
    class _BadReplace(_ReplaceChainAdapter):
        def replace_order(self, broker_order_id, req):
            self.replace_calls += 1
            raise RuntimeError("422 order not replaceable")
    ad = _BadReplace()
    _old, _new, resp, err = _run(ad)
    assert ad.replace_calls == 1
    assert resp is None and err.startswith("replace_failed")


# ── #4 force-fill cancel+place retry (RDGT srini) ─────────────────────────────
# The forced market close fired when a trader's working order FILLS. Its
# cancel+place had NO share-release retry (unlike _modify_place_one), so a single
# held_for_orders bounce stranded the subscriber long (prod RDGT 2026-08-10,
# ~2h until a manual close). Now it retries the place, same as the modify path.

# Don't actually sleep through the post-cancel settle in tests.
ce._FORCE_FILL_SETTLE_S = 0


def _run_ff(adapter, old=None):
    """Returns (old_id, new_id, resp, err, meta) — meta carries the fill the
    guard observed and the request it actually placed."""
    old = old or _OldCh()
    new_id = uuid.uuid4()
    return ce._force_fill_cancel_then_place((old, adapter, _req(), new_id))


def test_force_fill_retries_through_share_release_race():
    """First place bounces on 'insufficient qty', the retry succeeds → the forced
    close goes through instead of leaving the subscriber holding."""
    ad = _CancelPlaceAdapter(fail_first=2, final_filled="0")
    _old, _new, resp, err, _meta = _run_ff(ad)
    assert ad.cancelled and ad.place_calls == 3
    assert resp is not None and err is None


def test_force_fill_bails_when_cancel_is_noop():
    """cancel returned False (already terminal / likely filled) → never place a
    replacement (would double the position)."""
    ad = _CancelPlaceAdapter(cancel_result=False)
    _old, _new, resp, err, _meta = _run_ff(ad)
    assert ad.place_calls == 0
    assert resp is None and err == "cancel_noop_already_terminal"


def test_force_fill_gives_up_after_budget():
    """Race never clears within budget → place_failed (not an infinite loop)."""
    ad = _CancelPlaceAdapter(fail_first=99, final_filled="0")
    _old, _new, resp, err, _meta = _run_ff(ad)
    assert ad.place_calls == ce._MODIFY_PLACE_ATTEMPTS
    assert resp is None and err.startswith("place_failed")


# ── the double-buy guard (prod MSFT 2026-08) ──────────────────────────────────
# A cancel being ACCEPTED does not mean zero filled: Alpaca's cancel is async and
# a marketable-limit mirror can fill inside that window. Placing the full mirror
# size on top then DOUBLES the subscriber — a 5-share mirror filled 4 while a
# fresh 5 went on, leaving ~2x. So the path re-reads the order after a settle and
# places only the TRUE remainder. These tests had no coverage at all: the stubs
# they depend on had drifted, so all three force-fill tests errored out before
# reaching the guard.

def test_partial_fill_during_cancel_places_only_the_remainder():
    """The MSFT shape: mirror of 5, 4 filled while the cancel was in flight →
    place 1, not 5."""
    ad = _CancelPlaceAdapter(final_filled="4")
    _old, _new, resp, err, meta = _run_ff(ad, _OldCh(quantity="5"))
    assert err is None and resp is not None
    assert ad.placed_quantities == [Decimal("1")]
    assert meta["already"] == Decimal("4")


def test_fully_filled_during_cancel_places_nothing():
    """The whole mirror filled in the cancel window — anything placed here is
    pure duplication."""
    ad = _CancelPlaceAdapter(final_filled="5")
    _old, _new, resp, err, meta = _run_ff(ad, _OldCh(quantity="5"))
    assert ad.place_calls == 0
    assert resp is None and err == "cancel_but_filled"
    assert meta["already"] == Decimal("5")


def test_a_fill_already_known_to_our_row_is_honoured():
    """The guard takes the LARGER of our stored fill and the broker's re-read, so
    a stale broker response can't resurrect quantity we know already filled."""
    ad = _CancelPlaceAdapter(final_filled="0")
    _old, _new, _resp, err, meta = _run_ff(
        ad, _OldCh(quantity="5", filled_quantity="3"),
    )
    assert err is None
    assert ad.placed_quantities == [Decimal("2")]
    assert meta["already"] == Decimal("3")


def test_broker_reporting_more_filled_than_our_row_wins():
    ad = _CancelPlaceAdapter(final_filled="4")
    _old, _new, _resp, err, meta = _run_ff(
        ad, _OldCh(quantity="5", filled_quantity="1"),
    )
    assert err is None
    assert ad.placed_quantities == [Decimal("1")]
    assert meta["already"] == Decimal("4")


def test_unreadable_post_cancel_state_falls_back_to_our_row():
    """get_order failing must not abort the forced close — we size from what we
    know and still get the subscriber out."""
    ad = _CancelPlaceAdapter(final_filled=None)   # get_order raises
    _old, _new, resp, err, _meta = _run_ff(
        ad, _OldCh(quantity="5", filled_quantity="2"),
    )
    assert err is None and resp is not None
    assert ad.placed_quantities == [Decimal("3")]


def test_unfilled_mirror_places_the_full_size():
    """The ordinary case — nothing filled, so the whole mirror is forced."""
    ad = _CancelPlaceAdapter(final_filled="0")
    _old, _new, _resp, err, _meta = _run_ff(ad, _OldCh(quantity="5"))
    assert err is None
    assert ad.placed_quantities == [Decimal("5")]


def test_a_failed_cancel_never_places():
    """Cancel raised → the order's state is unknown, so placing could double.
    (WebullAdapter.cancel_order raises for exactly this reason when it cannot
    confirm the outcome.)"""
    class _RaisingCancel(_CancelPlaceAdapter):
        def cancel_order(self, broker_order_id):
            raise RuntimeError("webull cancel: state could not be confirmed")
    ad = _RaisingCancel()
    _old, _new, resp, err, meta = _run_ff(ad)
    assert ad.place_calls == 0
    assert resp is None and err.startswith("cancel_failed")
    assert meta is None


# ── AlpacaAdapter.replace_order builds a valid ReplaceOrderRequest ────────────
# Regression for the prod bug (NVDA stop, 07-29): a STOP mirror carries
# limit_price=0, and passing that to Alpaca's ReplaceOrderRequest raised
# "limit_price must be greater than 0", so stop-price modifies never propagated
# (they failed safe via order.mirror_replace_failed_kept_old). 0 must be treated
# as "unset".

class _FakeAlpacaClient:
    def __init__(self):
        self.captured = None
    def replace_order_by_id(self, order_id, order_data):
        self.captured = order_data
        class _R:
            id = "new-alpaca-id"
            status = "accepted"
            submitted_at = None
            filled_qty = "0"
            filled_avg_price = None
        return _R()


def _alpaca_with_fake():
    from app.brokers.alpaca import AlpacaAdapter
    ad = AlpacaAdapter({"api_key": "k", "api_secret": "s", "paper": True})
    ad._client = _FakeAlpacaClient()  # lazy client — inject a fake, no network
    return ad


def test_replace_stop_order_omits_zero_limit_price():
    """A stop mirror (limit_price=0, stop_price set) must NOT send limit_price=0
    to Alpaca — that's the validation error that blocked stop modifies."""
    ad = _alpaca_with_fake()
    stop_req = BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION,
        symbol="NVDA",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        quantity=Decimal("10"),
        limit_price=Decimal("0"),        # stop orders carry 0 here, not None
        stop_price=Decimal("0.45"),
        is_closing=True,
    )
    resp = ad.replace_order("old-id", stop_req)   # must not raise
    captured = ad._client.captured
    assert captured.limit_price is None            # zero limit dropped
    assert float(captured.stop_price) == 0.45      # stop leg preserved
    assert resp.broker_order_id == "new-alpaca-id"


def test_replace_limit_order_keeps_limit_price():
    """A normal limit mirror still sends its (non-zero) limit_price."""
    ad = _alpaca_with_fake()
    limit_req = BrokerOrderRequest(
        instrument_type=InstrumentType.OPTION,
        symbol="NVDA",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=Decimal("10"),
        limit_price=Decimal("0.55"),
        is_closing=True,
    )
    ad.replace_order("old-id", limit_req)
    captured = ad._client.captured
    assert float(captured.limit_price) == 0.55
    assert captured.stop_price is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll modify/replace tests passed.")
