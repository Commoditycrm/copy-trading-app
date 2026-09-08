"""Tests for the unfilled-order auto-cancel scanner (stale_order_canceller).

Two layers:
  1. Selection logic (in-memory SQLite, raw tables like test_multi_trader_follow):
     which WORKING copied mirrors are 'due' for auto-cancel.
  2. Integration (real Postgres + a monkeypatched fake broker, like
     test_fanout_close_through_pause): one stale mirror is actually cancelled +
     the subscriber notified, while a RETRY_PENDING order is left untouched.

Run standalone:  .venv/bin/python tests/test_unfilled_timeout_cancel.py
Or under pytest: pytest tests/test_unfilled_timeout_cancel.py
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, delete, select, text


# ── 1. Selection logic (SQLite, faithful mirror of the due-order WHERE) ───────
def _db():
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE subscriber_settings ("
            "user_id TEXT PRIMARY KEY, unfilled_timeout_enabled INT, "
            "unfilled_timeout_seconds INT)"
        ))
        c.execute(text(
            "CREATE TABLE orders (id TEXT PRIMARY KEY, user_id TEXT, "
            "parent_order_id TEXT, broker_account_id TEXT, broker_order_id TEXT, "
            "status TEXT, order_type TEXT, bracket_leg TEXT, submitted_at TEXT)"
        ))
        # Subscriber S: timeout ON, 60s.
        c.execute(text("INSERT INTO subscriber_settings VALUES ('S',1,60)"))
        # Subscriber D: timeout OFF.
        c.execute(text("INSERT INTO subscriber_settings VALUES ('D',0,60)"))
        old = "2000-01-01 00:00:00"   # far in the past → past due
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            # id, user, parent, acct, broker_order_id, status, order_type, bracket_leg, submitted_at
            ("stale_entry", "S", "p1", "a", "B1", "submitted", "limit", None, old),          # ✓ due
            ("stale_mkt",   "S", "p1", "a", "B1b", "accepted", "market", None, old),         # ✓ due
            ("stale_close", "S", "p2", "a", "B2", "partially_filled", "limit", None, old),   # ✓ due (close incl.)
            ("fresh",       "S", "p3", "a", "B3", "submitted", "limit", None, now),          # ✗ not old enough
            ("filled",      "S", "p4", "a", "B4", "filled", "limit", None, old),             # ✗ terminal
            ("no_broker",   "S", "p5", "a", None, "submitted", "limit", None, old),          # ✗ nothing to cancel
            ("no_acct",     "S", "p5b", None, "B5b", "submitted", "limit", None, old),       # ✗ broker gone
            ("not_mirror",  "S", None, "a", "B6", "submitted", "limit", None, old),          # ✗ trader's own order
            ("resting_stop","S", "p8", "a", "B8", "accepted", "stop", None, old),            # ✗ resting stop
            ("stop_limit",  "S", "p9", "a", "B9", "accepted", "stop_limit", None, old),      # ✗ resting stop-limit
            ("trailing",    "S", "pT", "a", "BT", "accepted", "trailing_stop", None, old),   # ✗ resting trailing
            ("bracket_tp",  "S", "pB", "a", "BB", "accepted", "limit", "tp", old),           # ✗ protective exit leg
            ("disabled",    "D", "p7", "a", "B7", "submitted", "limit", None, old),          # ✗ subscriber opted out
        ]
        for r in rows:
            c.execute(text(
                "INSERT INTO orders VALUES (:id,:u,:p,:acct,:b,:s,:ot,:bl,:t)"
            ), dict(zip(["id", "u", "p", "acct", "b", "s", "ot", "bl", "t"], r)))
    return eng


def _due(eng):
    with eng.begin() as c:
        return sorted(r[0] for r in c.execute(text(
            "SELECT o.id FROM orders o "
            "JOIN subscriber_settings ss ON ss.user_id = o.user_id "
            "WHERE o.parent_order_id IS NOT NULL "
            "  AND o.broker_account_id IS NOT NULL "
            "  AND o.broker_order_id IS NOT NULL "
            "  AND o.status IN ('pending','submitted','accepted','partially_filled') "
            "  AND o.order_type IN ('market','limit') "
            "  AND o.bracket_leg IS NULL "
            "  AND ss.unfilled_timeout_enabled = 1 "
            "  AND (julianday('now') - julianday(o.submitted_at)) * 86400 "
            "        >= ss.unfilled_timeout_seconds"
        )).fetchall())


def test_selection_picks_stale_entries_and_closes():
    assert _due(_db()) == ["stale_close", "stale_entry", "stale_mkt"]


def test_selection_excludes_fresh_filled_nonmirror_nobroker_disabled():
    picked = _due(_db())
    for excluded in ("fresh", "filled", "no_broker", "no_acct", "not_mirror", "disabled"):
        assert excluded not in picked


def test_selection_excludes_resting_stops_and_bracket_legs():
    """Resting stop-type orders and protective bracket exit legs must NOT be
    auto-cancelled — they rest by design."""
    picked = _due(_db())
    for excluded in ("resting_stop", "stop_limit", "trailing", "bracket_tp"):
        assert excluded not in picked


# ── 2. Integration (real Postgres, monkeypatched broker) ──────────────────────
def _run_integration():
    import app.services.stale_order_canceller as soc
    from app.database import SessionLocal
    from app.models.broker_account import BrokerAccount, BrokerName
    from app.models.notification import Notification
    from app.models.order import (
        InstrumentType, Order, OrderSide, OrderStatus, OrderType,
    )
    from app.models.settings import SubscriberSettings
    from app.models.user import User, UserRole

    # Hermetic broker: no real crypto / no real broker call.
    orig_adapter_for, orig_decrypt = soc.adapter_for, soc.decrypt_json
    soc.decrypt_json = lambda blob: {}
    soc.adapter_for = lambda acct, creds: SimpleNamespace(cancel_order=lambda boid: True)

    trader_id = uuid.uuid4()
    sub_id = uuid.uuid4()
    db = SessionLocal()
    try:
        db.add(User(id=trader_id, email=f"{trader_id.hex[:10]}@t.local", password_hash="x", role=UserRole.TRADER))
        db.add(User(id=sub_id, email=f"{sub_id.hex[:10]}@t.local", password_hash="x", role=UserRole.SUBSCRIBER))
        db.add(SubscriberSettings(
            user_id=sub_id, unfilled_timeout_enabled=True, unfilled_timeout_seconds=60,
        ))
        acct = BrokerAccount(
            id=uuid.uuid4(), user_id=sub_id, broker=BrokerName.FAKE,
            label="t", encrypted_credentials="x", connection_status="connected",
        )
        db.add(acct)
        db.flush()

        def _mk(status, *, parent, submitted, boid, is_closing=False):
            o = Order(
                id=uuid.uuid4(), user_id=sub_id, broker_account_id=acct.id,
                parent_order_id=parent, instrument_type=InstrumentType.STOCK,
                symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                quantity=Decimal("1"), status=status, broker_order_id=boid,
                submitted_at=submitted, is_closing=is_closing,
            )
            db.add(o)
            return o

        now = datetime.now(timezone.utc)
        parent = _mk(OrderStatus.FILLED, parent=None, submitted=now - timedelta(minutes=10), boid="P1")
        db.flush()
        stale = _mk(OrderStatus.SUBMITTED, parent=parent.id, submitted=now - timedelta(minutes=5), boid="C1")
        fresh = _mk(OrderStatus.SUBMITTED, parent=parent.id, submitted=now, boid="C2")
        retry = _mk(OrderStatus.RETRY_PENDING, parent=parent.id, submitted=now - timedelta(minutes=5), boid="C3")
        db.commit()  # persist — _cancel_one uses its own session

        # _due_order_ids sees the stale one, not the fresh/retry ones.
        due = set(soc._due_order_ids())
        assert stale.id in due, "stale mirror should be due"
        assert fresh.id not in due, "fresh mirror must not be due"
        assert retry.id not in due, "RETRY_PENDING must never be selected"

        # Cancel the stale one.
        assert soc._cancel_one(stale.id) == "cancelled"

        with SessionLocal() as v:
            c = v.get(Order, stale.id)
            assert c.status == OrderStatus.CANCELED, c.status
            assert c.closed_at is not None
            assert c.reject_reason and "Auto-cancelled" in c.reject_reason
            notifs = v.execute(select(Notification).where(
                Notification.user_id == sub_id,
                Notification.type == "copy.order_auto_cancelled_unfilled",
            )).scalars().all()
            assert len(notifs) == 1, notifs
            # RETRY_PENDING left untouched (composes with retry_scheduler).
            assert v.get(Order, retry.id).status == OrderStatus.RETRY_PENDING
    finally:
        soc.adapter_for, soc.decrypt_json = orig_adapter_for, orig_decrypt
        db.rollback()
        db.close()
        # Clean up: deleting the users cascades to orders / settings / accounts /
        # notifications (audit rows keep, SET NULL — harmless).
        with SessionLocal() as c:
            c.execute(delete(User).where(User.id.in_([trader_id, sub_id])))
            c.commit()


def test_integration_cancels_stale_mirror_and_notifies():
    _run_integration()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll unfilled-timeout tests passed.")
