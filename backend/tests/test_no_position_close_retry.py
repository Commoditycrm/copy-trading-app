"""Regression guard for the "no position to close" close-retry classifier.

SnapTrade's order-placement view can lag its positions view, so a copied CLOSE
fired promptly after an entry fills is rejected "no matching position" even
though the subscriber holds the contract (prod AAPL $337.5, Sep 2026 — closes
REJECTED while the broker held 3, stranding subscribers long). copy_engine now
routes such a rejection into the existing re-confirm-and-retry path (it only
retries when live_closeable_quantity confirms a held position, so a genuinely
flat account is never retried). This locks the classifier that gates it.

Pure-logic test — no DB, no broker.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.order_retry import is_no_position_close_error, is_order_conflict_error


def _e(msg):
    return RuntimeError(msg)


def test_snaptrade_no_position_rejections_are_recognized():
    for msg in [
        "SnapTrade place_mleg_order: Order rejected by brokerage - no position to close",
        "position not found on this account",
        "You hold no matching position to close.",
        "No open position for this contract",
    ]:
        assert is_no_position_close_error(_e(msg)) is True, msg


def test_liquidation_only_restriction_is_excluded():
    # Mentions positions, but it is NOT the transient order-vs-positions lag —
    # it's a durable account restriction, so it must NOT be retried as a close.
    msg = ("Your account has an option trading restriction and can only close "
           "existing positions; you will not be able to open new option positions.")
    assert is_no_position_close_error(_e(msg)) is False


def test_unrelated_errors_do_not_match():
    for msg in [
        "Insufficient buying power on this account.",
        "Request was throttled. Expected available in 1 second.",
        "wash trade: opposite side order exists",
        "asset is not fractionable",
    ]:
        assert is_no_position_close_error(_e(msg)) is False, msg


def test_no_position_is_distinct_from_a_conflict():
    # A pure "no position" rejection is not a same-contract conflict, and vice
    # versa — they route to the same retry block but for different reasons.
    no_pos = _e("no position to close")
    conflict = _e("wash trade: opposite side order exists")
    assert is_no_position_close_error(no_pos) and not is_order_conflict_error(no_pos)
    assert is_order_conflict_error(conflict) and not is_no_position_close_error(conflict)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
