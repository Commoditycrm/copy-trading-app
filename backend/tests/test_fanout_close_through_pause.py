"""Integration test for close-through-pause (mirror the trader's EXITS to
subscribers whose copy is paused, while still blocking new entries).

Drives the REAL ``copy_engine.fanout_async`` against the real Postgres schema.
Only the external edges are mocked — the broker REST call, the Redis-backed
subscriber/account cache lookups, credential decryption, and event publish.
Everything under test (the paused-follower SELECT, the entry-vs-close gate,
the held-quantity clamp) runs for real.

All DB writes happen inside one transaction that is ROLLED BACK at the end,
so the dev database is left untouched.

Run standalone:  .venv/bin/python tests/test_fanout_close_through_pause.py
Or under pytest: pytest tests/test_fanout_close_through_pause.py
"""
import asyncio
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.copy_engine as ce
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import (
    InstrumentType,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole


def _mk_user(db, role):
    u = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:12]}@test.local",
        password_hash="x",
        role=role,
    )
    db.add(u)
    return u


def _mk_acct(db, user_id):
    a = BrokerAccount(
        id=uuid.uuid4(),
        user_id=user_id,
        broker=BrokerName.FAKE,
        label="test",
        encrypted_credentials="x",
    )
    db.add(a)
    return a


def _mk_order(db, user_id, symbol, side, *, qty=10, is_closing=False, filled=None, status=OrderStatus.FILLED):
    o = Order(
        id=uuid.uuid4(),
        user_id=user_id,
        instrument_type=InstrumentType.STOCK,
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(str(qty)),
        is_closing=is_closing,
        status=status,
        filled_quantity=Decimal(str(filled)) if filled is not None else Decimal("0"),
    )
    db.add(o)
    return o


def _fake_place(item):
    """Stand-in for the broker REST call — always 'accepts' the order."""
    return SimpleNamespace(
        status=OrderStatus.SUBMITTED,
        broker_order_id="FAKE-" + item.child_order_id.hex[:8],
        submitted_at=datetime.now(timezone.utc),
        filled_quantity=Decimal("0"),
        filled_avg_price=None,
        bracket_legs=[],
    )


def _install_mocks(accts_by_user):
    async def _no_active_subs(db, trader_id):
        return []

    async def _accounts_for(db, user_id):
        return accts_by_user.get(user_id, [])

    async def _big_threshold():
        return 10_000

    ce.cache.get_subscribers_for_trader = _no_active_subs
    ce.cache.get_broker_accounts = _accounts_for
    ce.cache.decrypt_creds_cached = lambda acct_id, creds: b"x"
    ce.cache.invalidate_subscribers_for_trader = lambda *a, **k: None
    ce.get_fanout_batch_threshold_async = _big_threshold
    ce.adapter_for = lambda acct, creds: object()
    ce._place_mirror_with_conflict_resolve = _fake_place
    ce.events.publish = lambda *a, **k: None


def _statuses(results, user_id):
    return sorted(r.status for r in results if r.subscriber_user_id == user_id)


def run():
    db = SessionLocal()
    try:
        trader = _mk_user(db, UserRole.TRADER)
        sub_holds = _mk_user(db, UserRole.SUBSCRIBER)     # paused, HOLDS the symbol
        sub_flat = _mk_user(db, UserRole.SUBSCRIBER)      # paused, holds nothing
        db.flush()

        # Both subscribers follow the trader with copy DISABLED (paused).
        for u in (sub_holds, sub_flat):
            db.add(SubscriberSettings(
                user_id=u.id,
                following_trader_id=trader.id,
                copy_enabled=False,
                multiplier=Decimal("1.000"),
            ))
        acct_holds = _mk_acct(db, sub_holds.id)
        acct_flat = _mk_acct(db, sub_flat.id)

        # The trader holds 20 AAPL (filled BUY) and will sell 10 — a partial
        # exit, so even after the sell nets out, their position is still long.
        # That keeps trader_closing True with is_closing=False (how SnapTrade
        # reports a stock close), which is what exercises the entry-gate path.
        _mk_order(db, trader.id, "AAPL", OrderSide.BUY, qty=20, filled=20)
        # The holding subscriber holds 10 AAPL.
        _mk_order(db, sub_holds.id, "AAPL", OrderSide.BUY, filled=10)
        db.flush()

        _install_mocks({sub_holds.id: [acct_holds], sub_flat.id: [acct_flat]})

        # ── Scenario 1 & 3: trader CLOSES AAPL (SELL, no is_closing flag) ──────
        sell = _mk_order(db, trader.id, "AAPL", OrderSide.SELL, is_closing=False,
                         filled=10, status=OrderStatus.FILLED)
        db.flush()
        results = asyncio.run(ce.fanout_async(db, sell, trader))

        holds_status = _statuses(results, sub_holds.id)
        flat_status = _statuses(results, sub_flat.id)
        assert holds_status == ["submitted"], f"paused holder should receive the close, got {holds_status}"
        assert flat_status == ["skipped_paused_entry"], f"paused non-holder must be blocked, got {flat_status}"

        # And a real close child row was created for the holder, flagged closing.
        child = db.query(Order).filter(
            Order.user_id == sub_holds.id,
            Order.parent_order_id == sell.id,
        ).one()
        assert child.is_closing is True, "mirrored child must be a CLOSE"
        assert child.side == OrderSide.SELL
        print("PASS  paused holder receives the trader's close; paused non-holder blocked")

        # ── Scenario 2: trader OPENS a NEW position (BUY MSFT) ────────────────
        buy = _mk_order(db, trader.id, "MSFT", OrderSide.BUY, is_closing=False,
                        filled=10, status=OrderStatus.FILLED)
        db.flush()
        results2 = asyncio.run(ce.fanout_async(db, buy, trader))

        assert _statuses(results2, sub_holds.id) == [], "paused sub must NOT receive a new entry"
        assert _statuses(results2, sub_flat.id) == [], "paused sub must NOT receive a new entry"
        n_children = db.query(Order).filter(Order.parent_order_id == buy.id).count()
        assert n_children == 0, "no mirror child should exist for a new entry while paused"
        print("PASS  new entry (BUY) is NOT mirrored to paused subscribers")

        print("\nAll close-through-pause tests passed.")
    finally:
        db.rollback()  # leave the dev DB untouched
        db.close()


def test_close_through_pause():
    run()


if __name__ == "__main__":
    run()
