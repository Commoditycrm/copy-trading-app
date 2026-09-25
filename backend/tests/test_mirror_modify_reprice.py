"""Repricing a trader's Discord entry must reprice the mirrors, not lose them.

Live 2026-09-25, SPY 770C: the trader's order moved 0.25 -> 0.28, and the
subscriber's mirror came back CANCELED with

    order.mirror_modify_failed
      error: replace_failed: {"code":40010001,"message":"client_order_id must be unique"}
      old_order_lost: true

The replacement reused ``str(child.id)`` — the id the mirror being replaced was
still resting under. The cancel had already succeeded by then, so the mirror was
simply gone while the trader stayed in the trade. The same defect was fixed for
the TRADER's own order in discord_reprice._replace and never carried across.
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import copy_engine

_SRC = inspect.getsource(copy_engine.propagate_modify_to_mirrors)


def test_the_replacement_does_not_reuse_the_mirrors_own_id():
    """The exact live failure. Alpaca rejects a client_order_id that the
    just-cancelled order still holds, and by then the mirror is already gone."""
    assert "client_order_id=str(child.id)" not in _SRC
    assert "client_order_id=str(new_coid)" in _SRC


def test_the_replacement_id_is_fresh_per_mirror():
    """Generated inside the per-child loop. One id shared across a fanout would
    fail for every mirror after the first."""
    loop_at = _SRC.index("for child in children:")
    body = _SRC[loop_at:_SRC.index("if not pending:")]
    assert "new_coid = uuid.uuid4()" in body


def test_the_replacement_id_is_marked_app_originated():
    """The broker echoes client_order_id back on its order stream. Without the
    marker the listener reads the replacement as an externally-placed trade and
    inserts a duplicate parent row plus a second fanout."""
    assert "order_intent.mark_app_originated(new_coid)" in _SRC
    # BEFORE the request is built, not after the broker call.
    assert _SRC.index("mark_app_originated") < _SRC.index("pending.append")


def test_an_atomic_replace_is_preferred_over_cancel_then_place():
    """Cancel+place leaves the subscriber orderless in the gap and strands them
    outright if the place fails — which is precisely what happened."""
    assert 'getattr(ad, "supports_replace", False)' in _SRC
    replace_at = _SRC.index('getattr(ad, "supports_replace", False)')
    cancel_at = _SRC.index("ad.cancel_order(ch.broker_order_id)")
    assert replace_at < cancel_at, "the atomic path must be tried first"


def test_a_pending_replace_chain_is_retried():
    """Alpaca models a modify as a replacement CHAIN; a re-replace fired before
    the previous one settles fails transiently with 42210000. It settles in
    ~1-2s, so retrying the same replace is the fix — without it a re-priced
    mirror is left at stale terms while the trader has moved on."""
    assert "is_replace_chain_pending_error(exc)" in _SRC
    assert "_MODIFY_PLACE_BACKOFF_S" in _SRC


def test_a_failed_atomic_replace_does_not_mark_the_mirror_lost():
    """The atomic contract: on failure the ORIGINAL order is left working
    untouched. Marking it canceled would report a live order as gone — the
    mirror image of the bug being fixed."""
    assert 'replace_chain_failed' in _SRC
    # `lost` is what flips the row to CANCELED, and it keys on this prefix.
    assert 'err.startswith("replace_failed")' in _SRC
    assert not re.search(r'startswith\("replace_chain_failed"\)', _SRC)


def test_a_cancel_failure_still_leaves_the_old_order_alone():
    """A cancel failure almost always means the mirror just filled. Placing the
    replacement then would stack an order on top of a fill."""
    guard = _SRC[_SRC.index("ad.cancel_order(ch.broker_order_id)"):][:400]
    assert "cancel_failed" in guard
    assert "return" in guard
