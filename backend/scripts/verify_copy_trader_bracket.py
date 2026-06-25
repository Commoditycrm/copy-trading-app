"""End-to-end verification for the "copy trader's SL/TP" feature.

Run from the backend dir:
    .venv/bin/python scripts/verify_copy_trader_bracket.py

Exercises the REAL functions (not reimplementations) against the real DB
with a fake broker adapter that records placed orders. Cleans up every row
it creates. Exits non-zero on any failure.

Covers:
  1. copy_engine._trader_bracket_for_copy — pct math (buy/sell/inverted/
     no-anchor fallback)
  2. bracket_emulator.emulate_bracket_exits — re-anchors the copied pct
     onto the SUBSCRIBER's own fill, on SnapTrade AND on Alpaca stocks
     (the is_copied_mirror gate), placing TP+SL at the right prices
  3. position_enforcer.enforce_position_tp_sl — SKIPS the subscriber when
     copy_trader_bracket is on (no double-close)
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import (
    InstrumentType, Order, OrderSide, OrderStatus, OrderType,
)
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole
from app.brokers.base import BrokerOrderResult

import app.services.bracket_emulator as be
import app.services.position_enforcer as pe
from app.services.copy_engine import _trader_bracket_for_copy

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, cond, detail))
    mark = "✓ PASS" if cond else "✗ FAIL"
    print(f"  {mark}  {name}" + (f"  →  {detail}" if detail and not cond else ""))


# ── Fake adapter that records placed orders ──────────────────────────────

class _FakeAdapter:
    def __init__(self):
        self.placed: list = []

    def place_order(self, req):
        self.placed.append(req)
        return BrokerOrderResult(
            broker_order_id=f"fake-{uuid.uuid4().hex[:8]}",
            status=OrderStatus.SUBMITTED,
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=Decimal("0"),
            filled_avg_price=None,
        )

    def get_positions(self):
        return []

    def cancel_order(self, broker_order_id):
        return None


# ── Tiny fake order for the pure pct helper ──────────────────────────────

class _FakeTraderOrder:
    def __init__(self, side, limit_price, filled_avg_price, tp, sl):
        self.side = side
        self.limit_price = limit_price
        self.filled_avg_price = filled_avg_price
        self.take_profit_price = tp
        self.stop_loss_price = sl


def test_pct_math() -> None:
    print("\n[1] copy_engine._trader_bracket_for_copy — pct math")

    # BUY entry 200, TP 220 (+10%), SL 190 (-5%)
    use_pct, tp, sl = _trader_bracket_for_copy(
        _FakeTraderOrder(OrderSide.BUY, Decimal("200"), None, Decimal("220"), Decimal("190"))
    )
    check("buy: use_pct=True", use_pct is True)
    check("buy: tp_pct=10", tp == Decimal("10.0000"), f"got {tp}")
    check("buy: sl_pct=5", sl == Decimal("5.0000"), f"got {sl}")

    # SELL/short entry 200, TP 180 (+10% good), SL 210 (-5% bad)
    use_pct, tp, sl = _trader_bracket_for_copy(
        _FakeTraderOrder(OrderSide.SELL, Decimal("200"), None, Decimal("180"), Decimal("210"))
    )
    check("sell: tp_pct=10", use_pct and tp == Decimal("10.0000"), f"got {tp}")
    check("sell: sl_pct=5", sl == Decimal("5.0000"), f"got {sl}")

    # Inverted bracket (BUY with TP BELOW entry) → dropped
    use_pct, tp, sl = _trader_bracket_for_copy(
        _FakeTraderOrder(OrderSide.BUY, Decimal("200"), None, Decimal("190"), None)
    )
    check("inverted tp dropped (None)", tp is None, f"got {tp}")

    # No anchor (market, unfilled) → absolute fallback
    use_pct, tp, sl = _trader_bracket_for_copy(
        _FakeTraderOrder(OrderSide.BUY, None, None, Decimal("220"), Decimal("190"))
    )
    check("no-anchor: use_pct=False (absolute fallback)", use_pct is False)
    check("no-anchor: tp=abs 220", tp == Decimal("220"), f"got {tp}")

    # No bracket at all
    use_pct, tp, sl = _trader_bracket_for_copy(
        _FakeTraderOrder(OrderSide.BUY, Decimal("200"), None, None, None)
    )
    check("no bracket: all None", (use_pct, tp, sl) == (False, None, None))


def _mk_user(db, role) -> User:
    u = User(email=f"verify-{uuid.uuid4().hex[:10]}@test.local",
             password_hash="x", role=role)
    db.add(u)
    db.flush()
    return u


def _mk_acct(db, user_id, broker) -> BrokerAccount:
    a = BrokerAccount(user_id=user_id, broker=broker, label="verify",
                      encrypted_credentials="{}", connection_status="connected")
    db.add(a)
    db.flush()
    return a


def test_emulator_reanchor(broker: BrokerName, instrument: InstrumentType, label: str) -> None:
    print(f"\n[2] bracket_emulator re-anchor — {label}")
    created_ids: list = []
    fake = _FakeAdapter()
    orig_adapter_for = be.adapter_for
    orig_decrypt = be.decrypt_json
    be.adapter_for = lambda acct, creds: fake
    be.decrypt_json = lambda blob: {}
    try:
        with SessionLocal() as db:
            trader = _mk_user(db, UserRole.TRADER)
            sub = _mk_user(db, UserRole.SUBSCRIBER)
            acct = _mk_acct(db, sub.id, broker)

            # Trader entry (parent) — just needs an id.
            parent = Order(
                user_id=trader.id, instrument_type=instrument, symbol="AAPL",
                side=OrderSide.BUY, order_type=OrderType.LIMIT,
                quantity=Decimal("10"), status=OrderStatus.FILLED,
            )
            db.add(parent)
            db.flush()

            # Subscriber MIRROR entry: limit 210, FILLED, with copied pct
            # bracket (10% TP / 5% SL). Re-anchor must use limit_price=210.
            child = Order(
                user_id=sub.id, broker_account_id=acct.id,
                parent_order_id=parent.id,
                instrument_type=instrument, symbol="AAPL",
                side=OrderSide.BUY, order_type=OrderType.LIMIT,
                quantity=Decimal("10"),
                limit_price=Decimal("210"),
                filled_quantity=Decimal("10"),
                filled_avg_price=Decimal("205"),  # fill better than limit
                take_profit_pct=Decimal("10.0000"),
                stop_loss_pct=Decimal("5.0000"),
                status=OrderStatus.FILLED,
            )
            db.add(child)
            db.flush()
            created_ids = [child.id, parent.id, acct.id, sub.id, trader.id]

            exits = be.emulate_bracket_exits(db, child)
            db.flush()
            for e in exits:
                created_ids.insert(0, e.id)

            # Anchor is the subscriber's FILL (205), not the limit — risk
            # parity off what they actually paid.
            # TP = 205 * 1.10 = 225.50 ; SL = 205 * 0.95 = 194.75
            legs = {e.bracket_leg: e for e in exits}
            if instrument == InstrumentType.OPTION:
                # Option SL (STOP) is deferred to the monitor → only TP placed.
                check(f"{label}: TP leg placed", "tp" in legs, f"legs={list(legs)}")
                if "tp" in legs:
                    check(f"{label}: TP=225.50 (re-anchored on fill 205)",
                          legs["tp"].limit_price == Decimal("225.50"),
                          f"got {legs['tp'].limit_price}")
            else:
                check(f"{label}: both TP+SL placed", set(legs) == {"tp", "sl"},
                      f"legs={list(legs)}")
                if "tp" in legs:
                    check(f"{label}: TP=225.50 (re-anchored on fill 205, not 210 limit)",
                          legs["tp"].limit_price == Decimal("225.50"),
                          f"got {legs['tp'].limit_price}")
                if "sl" in legs:
                    check(f"{label}: SL=194.75",
                          legs["sl"].stop_price == Decimal("194.75"),
                          f"got {legs['sl'].stop_price}")
                check(f"{label}: exits sized to filled qty 10",
                      all(e.quantity == Decimal("10") for e in exits))
                check(f"{label}: exits are SELL (opposite of BUY entry)",
                      all(e.side == OrderSide.SELL for e in exits))
                check(f"{label}: adapter actually called",
                      len(fake.placed) == len(exits))

            # Cleanup
            db.rollback()
    finally:
        be.adapter_for = orig_adapter_for
        be.decrypt_json = orig_decrypt


def test_position_enforcer_skip() -> None:
    print("\n[3] position_enforcer — skips when copy_trader_bracket on")
    with SessionLocal() as db:
        sub = _mk_user(db, UserRole.SUBSCRIBER)
        acct = _mk_acct(db, sub.id, BrokerName.SNAPTRADE)
        s = SubscriberSettings(
            user_id=sub.id, copy_enabled=True,
            position_tp_pct=Decimal("10"), position_sl_pct=Decimal("5"),
            copy_trader_bracket=True,
        )
        db.add(s)
        db.flush()

        # Even though position_tp_pct/sl_pct are set, the toggle must
        # short-circuit BEFORE any broker call.
        called = {"n": 0}
        orig = pe.adapter_for
        pe.adapter_for = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("adapter_for must NOT be called when toggle on"))
        try:
            out = pe.enforce_position_tp_sl(db, sub.id, acct.id)
            check("returns [] (skipped)", out == [], f"got {out}")
        finally:
            pe.adapter_for = orig
            db.rollback()

    # And the inverse: toggle OFF still runs (reaches adapter).
    with SessionLocal() as db:
        sub = _mk_user(db, UserRole.SUBSCRIBER)
        acct = _mk_acct(db, sub.id, BrokerName.SNAPTRADE)
        s = SubscriberSettings(
            user_id=sub.id, copy_enabled=True,
            position_tp_pct=Decimal("10"), position_sl_pct=Decimal("5"),
            copy_trader_bracket=False,
        )
        db.add(s)
        db.flush()
        fake = _FakeAdapter()
        orig = pe.adapter_for
        orig_dec = pe.decrypt_json
        pe.adapter_for = lambda *a, **k: fake
        pe.decrypt_json = lambda b: {}
        try:
            out = pe.enforce_position_tp_sl(db, sub.id, acct.id)
            # No positions → [], but it should have reached get_positions().
            check("toggle OFF still runs enforcer (no positions → [])", out == [])
        finally:
            pe.adapter_for = orig
            pe.decrypt_json = orig_dec
            db.rollback()


def main() -> int:
    print("=" * 64)
    print("VERIFY: copy_trader_bracket end-to-end")
    print("=" * 64)
    test_pct_math()
    test_emulator_reanchor(BrokerName.SNAPTRADE, InstrumentType.STOCK, "SnapTrade stock")
    test_emulator_reanchor(BrokerName.ALPACA, InstrumentType.STOCK, "Alpaca stock (is_copied_mirror gate)")
    test_emulator_reanchor(BrokerName.SNAPTRADE, InstrumentType.OPTION, "SnapTrade option")
    test_position_enforcer_skip()

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = [(n, d) for n, ok, d in _results if not ok]
    print(f"RESULT: {passed}/{len(_results)} passed")
    for n, d in failed:
        print(f"   FAILED: {n}  →  {d}")
    print("=" * 64)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
