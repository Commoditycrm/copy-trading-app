"""Regression test for the fanout close-clamp fill-sync race.

Reproduces the prod incident (22-Jul, NDXP on Webull): a subscriber held an
option (entry FILLED at the broker) but its ``filled_quantity`` hadn't synced
into our numbers yet, so ``_closeable_quantity`` read 0 and the trader's close
was wrongly skipped for the subscriber (``copy.skipped_zero_qty``) while the
trader had already exited.

The fix: when the trader is genuinely closing and the subscriber has a
same-contract entry the broker has FILLED (even if our qty lags), don't drop the
close — keep it and force close semantics, letting the broker be the arbiter.

Runs standalone (``.venv/bin/python tests/test_fanout_close_clamp.py``) or under
pytest. Uses a real in-memory SQLite session against the actual ORM queries —
only the ``orders`` table is created (audit_logs uses JSONB, which SQLite can't
render, and we don't need it here).
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal

# Allow running standalone (python tests/test_fanout_close_clamp.py) — put the
# backend package root (parent of tests/) on the path. pytest handles this on
# its own via rootdir, so this is a no-op there.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.services.copy_engine as ce
from app.models.order import (
    InstrumentType,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)

_EXP = date(2026, 7, 22)


def _session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    Order.__table__.create(eng)  # only the table under test; skip JSONB audit_logs
    return Session(eng)


def _add(db, user_id, side, status, filled_q, *, is_closing=False, strike="29150"):
    o = Order(
        id=uuid.uuid4(),
        user_id=user_id,
        broker_account_id=uuid.uuid4(),
        instrument_type=InstrumentType.OPTION,
        symbol="NDXP",
        option_expiry=_EXP,
        option_strike=Decimal(strike),
        option_right=None,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        is_closing=is_closing,
        status=status,
        filled_quantity=Decimal(str(filled_q)),
    )
    db.add(o)
    db.flush()
    return o


def _incoming_sell(user_id):
    """The trader's SELL close, as the fanout evaluates it per-subscriber."""
    return Order(
        id=uuid.uuid4(),
        user_id=user_id,
        instrument_type=InstrumentType.OPTION,
        symbol="NDXP",
        option_expiry=_EXP,
        option_strike=Decimal("29150"),
        option_right=None,
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        is_closing=False,  # SnapTrade never sets this for Webull
        status=OrderStatus.FILLED,
        filled_quantity=Decimal("1"),
    )


def _keep_close(db, sub_id, sell, trader_closing):
    """Mirror of the fanout decision: keep a zero-held close (don't skip)?"""
    closeable = ce._closeable_quantity(db, sub_id, sell, subtract_reserved=False)
    entry_landing = ce._has_working_entry_for_contract(db, sub_id, sell)
    entry_filled_unsynced = trader_closing and ce._has_filled_entry_for_contract(
        db, sub_id, sell
    )
    return closeable <= 0 and (entry_landing or entry_filled_unsynced)


def test_entry_filled_but_qty_unsynced_is_not_skipped():
    """The 22-Jul NDXP bug: entry FILLED, filled_quantity still 0, trader closing."""
    db = _session()
    sub = uuid.uuid4()
    _add(db, sub, OrderSide.BUY, OrderStatus.FILLED, 0)  # broker filled it; our qty lags
    sell = _incoming_sell(uuid.uuid4())

    assert ce._closeable_quantity(db, sub, sell, subtract_reserved=False) == 0
    assert ce._has_working_entry_for_contract(db, sub, sell) is False  # old guard missed it
    assert ce._has_filled_entry_for_contract(db, sub, sell) is True    # new guard catches it
    assert _keep_close(db, sub, sell, trader_closing=True) is True      # -> close is NOT skipped


def test_rejected_entry_is_still_cleanly_skipped():
    """Alpaca can't trade the index option: entry REJECTED, never held -> skip."""
    db = _session()
    sub = uuid.uuid4()
    _add(db, sub, OrderSide.BUY, OrderStatus.REJECTED, 0)
    sell = _incoming_sell(uuid.uuid4())

    assert ce._has_filled_entry_for_contract(db, sub, sell) is False
    assert _keep_close(db, sub, sell, trader_closing=True) is False  # unchanged: clean skip


def test_genuinely_held_needs_no_special_path():
    """Fully synced position: closeable reflects the holding, no clamp needed."""
    db = _session()
    sub = uuid.uuid4()
    _add(db, sub, OrderSide.BUY, OrderStatus.FILLED, 1)
    sell = _incoming_sell(uuid.uuid4())

    assert ce._closeable_quantity(db, sub, sell, subtract_reserved=False) == 1


def test_not_kept_when_trader_is_not_closing():
    """Guard: without the trader actually closing, a filled entry alone doesn't
    force a close (avoids acting on a stale-0 read for a non-close)."""
    db = _session()
    sub = uuid.uuid4()
    _add(db, sub, OrderSide.BUY, OrderStatus.FILLED, 0)
    sell = _incoming_sell(uuid.uuid4())

    assert _keep_close(db, sub, sell, trader_closing=False) is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll close-clamp race tests passed.")
