"""A take-profit that filled with nobody hearing of it is a trim, not a resize.

QA 2026-10-19, QQQ 722P, a Discord subscriber on Webull (no order listener):
the entry bought 10 @ 0.967 and Trim 1 (75%) went out as a plain limit for 8 @
1.22 — the market was already there — and filled 8 @ 1.22 at 12:01:13. Our row
still read SUBMITTED. The next pass saw 2 held, re-planned Trim 1 as "75% of 2
= 2", and cancelled the 8 to replace it; the cancel could not be confirmed and
was recorded as a cancel. Trim 1 then sold the 2 left for Trim 2, and the 8 @
1.22 was missing from the timeline, Closed today and the day's P&L.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_take_profit as tp
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OrderType, OptionRight
from app.models.user import User, UserRole
from app.services import exit_confirm, fills_sync

NOW = datetime(2026, 10, 19, 16, 1, 20, tzinfo=timezone.utc)


def _ts(**kw):
    """The subscriber's ladder: T1 +25% sell 75% stop 0%, T2 +50% sell 50%."""
    base = dict(
        discord_trim_profit_gate_pct=D("25"), discord_trim_stop_pct=D("0"), discord_trim_qty_pct=D("75"),
        discord_trim2_profit_gate_pct=D("50"), discord_trim2_stop_pct=D("0"), discord_trim2_qty_pct=D("50"),
        discord_trim3_profit_gate_pct=None, discord_trim3_stop_pct=None, discord_trim3_qty_pct=None,
        discord_trim_count=2, discord_extra_trims=[], discord_fill_stop_pct=D("-50"),
        discord_trim_price_threshold=D("0.90"), discord_trim_trail_amount=D("0.25"),
        discord_auto_trim=False, discord_manual_exit=False, discord_tp_orders=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _guard(**kw):
    base = dict(
        symbol="QQQ", closed_at=None, entry_price=D("0.97"), sell_count=0, stop_price=D("0.48"),
        stop_trail_pct=None, stop_peak=None,
        trail_qty=None, trail_amount=None, peak_price=None, stop_order_id=None,
        tp_order_id=None, tp_stop_order_id=None, tp_rung=None, tp_qty=None, tp_off=False,
        tp_backoff_until=None, user_id=uuid.uuid4(),
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _Book:
    """Our order rows, and a broker whose state our rows only learn by asking."""

    def __init__(self):
        self.orders, self.calls, self.broker = {}, [], {}
        self.readable = True

    def get(self, _model, oid):
        return self.orders.get(oid)

    def _new(self, **kw):
        o = SimpleNamespace(**{**dict(id=uuid.uuid4(), status=OrderStatus.SUBMITTED, filled_quantity=D(0),
                                      filled_avg_price=None, limit_price=None, stop_price=None,
                                      submitted_at=NOW, closed_at=None, broker_filled_at=None), **kw})
        self.orders[o.id] = o
        return o

    def place_limit(self, qty, price):
        self.calls.append(("limit", qty, price))
        return self._new(quantity=qty, limit_price=price).id

    def place_pair(self, qty, price, stop):
        self.calls.append(("pair", qty, price, stop))
        return self._new(quantity=qty, limit_price=price).id, self._new(quantity=qty, stop_price=stop).id

    def cancel(self, oid):
        self.calls.append(("cancel", oid))
        self.orders[oid].status = OrderStatus.CANCELED

    def fills_at_broker(self, oid, price):
        """Filled at the broker — our row is not told."""
        self.broker[oid] = (OrderStatus.FILLED, self.orders[oid].quantity, D(price))

    def refresh(self, oid):
        self.calls.append(("read", oid))
        if not self.readable:
            return False
        o = self.orders[oid]
        if oid in self.broker:
            o.status, o.filled_quantity, o.filled_avg_price = self.broker[oid]
        return True


def _run(book, g, held, *, mark="1.25", now=NOW, refresh=True):
    seen = {}

    def _stop():
        seen["stop"] = g.tp_qty
        return "in sync"

    out = tp.reconcile(book, g, D(held), _ts(), D(mark) if mark else None,
                       place_limit=book.place_limit, place_pair=book.place_pair, cancel=book.cancel,
                       reconcile_stop=_stop, refresh=book.refresh if refresh else None,
                       now=now)
    return out, seen


def _placed(book):
    return [c for c in book.calls if c[0] in ("limit", "pair")]


def test_the_incident_trim_one_is_credited_and_trim_two_rests_for_half_of_what_is_left():
    book, g = _Book(), _guard()
    _run(book, g, 10)
    assert _placed(book) == [("limit", D(8), D("1.22"))]            # 75% of 10 = 7.5 → 8, at the market already
    t1 = g.tp_order_id
    book.fills_at_broker(t1, "1.22")

    out, _ = _run(book, g, 2, mark="1.26")                          # Webull now reports 2 held

    assert book.orders[t1].status == OrderStatus.FILLED, "the 8 @ 1.22 is a fill, not a cancel"
    assert ("cancel", t1) not in book.calls
    assert g.sell_count == 1 and "trim 1 filled" in out
    assert g.stop_price == D("0.97")                                # Trim 1's stop: break-even, on the 2
    assert _placed(book)[-1] == ("pair", D(1), D("1.46"), D("0.97")), "Trim 2: 50% of 2, at +50%"
    assert not [c for c in _placed(book) if c[1] == D(2)], "the 2 are never sold as Trim 1 again"


def test_while_the_broker_cannot_be_read_nothing_is_cancelled_or_resized():
    book, g = _Book(), _guard()
    _run(book, g, 10)
    t1 = g.tp_order_id
    book.readable = False
    out, seen = _run(book, g, 2, mark="1.26")
    assert "take-profit kept" in out and "could not be read" in out
    assert ("cancel", t1) not in book.calls and len(_placed(book)) == 1
    assert "stop" not in seen, "the stop is left as it is too"
    assert g.tp_order_id == t1 and g.sell_count == 0
    # Readable again: it is credited then.
    book.readable = True
    book.fills_at_broker(t1, "1.22")
    _run(book, g, 2, mark="1.26")
    assert g.sell_count == 1


def test_working_for_more_than_is_held_waits_for_the_broker_to_say_filled():
    """Webull's order detail can trail the position. A take-profit for 8 with
    2 held can only have filled — it reserves its 8."""
    book, g = _Book(), _guard()
    _run(book, g, 10)
    t1 = g.tp_order_id
    out, _ = _run(book, g, 2, mark="1.26")                          # still "working" at the broker
    assert "take-profit kept" in out and "fewer than its 8" in out
    assert ("cancel", t1) not in book.calls
    book.fills_at_broker(t1, "1.22")
    _run(book, g, 2, mark="1.26", now=NOW + timedelta(seconds=10))
    assert g.sell_count == 1


def test_after_the_wait_a_take_profit_still_working_is_replaced():
    book, g = _Book(), _guard()
    _run(book, g, 10)
    t1 = g.tp_order_id
    _run(book, g, 2, mark="1.26", now=NOW + tp.LOOKS_FILLED_WAIT + timedelta(seconds=1))
    assert ("cancel", t1) in book.calls


def test_a_real_resize_is_still_replaced_at_once():
    """An add doubled the position: the take-profit is smaller than what is
    held, so nothing says it filled."""
    book, g = _Book(), _guard(stop_price=None)
    _run(book, g, 10, mark="1.00")
    t1 = g.tp_order_id
    out, _ = _run(book, g, 20, mark="1.00")
    assert ("cancel", t1) in book.calls and "next pass" in out
    _run(book, g, 20, mark="1.00")
    assert _placed(book)[-1] == ("limit", D(15), D("1.22"))


def test_without_a_refresher_it_behaves_as_before():
    book, g = _Book(), _guard(stop_price=None)
    _run(book, g, 10, mark="1.00")
    t1 = g.tp_order_id
    _run(book, g, 2, mark="1.00", refresh=False)
    assert ("cancel", t1) in book.calls


# ── an unconfirmed cancel keeps the order ────────────────────────────────────

def test_release_keeps_an_order_whose_cancel_was_not_confirmed():
    book, g = _Book(), _guard(stop_price=None)
    _run(book, g, 10, mark="1.00")
    t1 = g.tp_order_id
    assert tp.release(book, g, lambda oid: None) is True            # the canceller left it working
    assert g.tp_order_id == t1, "forgetting it is how the fill was lost"


def test_the_pass_after_an_unconfirmed_cancel_earmarks_what_actually_rests():
    book, g = _Book(), _guard(stop_price=None)
    _run(book, g, 10, mark="1.00")
    book.cancel = lambda oid: book.calls.append(("cancel", oid))     # accepted by nobody
    out, seen = _run(book, g, 20, mark="1.00")
    assert "cancel not confirmed" in out
    assert seen["stop"] == D(8), "the stop is sized around the 8 still resting, not the new plan's 15"
    assert len(_placed(book)) == 1


# ── exit_confirm ─────────────────────────────────────────────────────────────

class _Adapter:
    def __init__(self, result=None, raises=False, cancel=None):
        self.result, self.raises, self._cancel, self.reads = result, raises, cancel, 0

    def get_order(self, _id):
        self.reads += 1
        if self.raises:
            raise RuntimeError("429 Too Many Requests")
        return self.result

    def cancel_order(self, _id):
        if self._cancel is None:
            raise RuntimeError("webull cancel: request accepted but its state could not be confirmed")
        return self._cancel


def _row(**kw):
    base = dict(id=uuid.uuid4(), status=OrderStatus.SUBMITTED, broker_order_id="b1",
                filled_quantity=D(0), filled_avg_price=None, broker_filled_at=None, closed_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_an_unreadable_cancel_is_left_working_when_asked():
    o = _row()
    exit_confirm.cancel_and_record(_Adapter(raises=True), o, unconfirmed=None)
    assert o.status == OrderStatus.SUBMITTED


def test_by_default_an_unreadable_cancel_still_reads_as_cancelled():
    o = _row()
    exit_confirm.cancel_and_record(_Adapter(raises=True), o)
    assert o.status == OrderStatus.CANCELED


def test_refresh_records_a_fill_from_the_broker():
    o = _row()
    at = datetime(2026, 10, 19, 16, 1, 13, tzinfo=timezone.utc)
    res = SimpleNamespace(status=OrderStatus.FILLED, filled_quantity=D(8), filled_avg_price=D("1.22"), filled_at=at)
    assert exit_confirm.refresh_from_broker(_Adapter(res), o) is True
    assert (o.status, o.filled_quantity, o.filled_avg_price, o.closed_at) == (OrderStatus.FILLED, D(8), D("1.22"), at)


def test_refresh_that_cannot_read_changes_nothing():
    o = _row()
    assert exit_confirm.refresh_from_broker(_Adapter(raises=True), o) is False
    assert o.status == OrderStatus.SUBMITTED


# ── a fill already recorded as a cancel is found ─────────────────────────────

def _setup(db, *, status=OrderStatus.CANCELED, order_type=OrderType.LIMIT, closed_at=None):
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local",
                password_hash="x", role=UserRole.SUBSCRIBER)
    db.add(user)
    acct = BrokerAccount(id=uuid.uuid4(), user_id=user.id, broker=BrokerName.FAKE,
                         label="test", encrypted_credentials="x")
    db.add(acct)
    db.flush()
    order = Order(
        id=uuid.uuid4(), user_id=user.id, broker_account_id=acct.id,
        instrument_type=InstrumentType.OPTION, symbol="QQQ",
        option_strike=D("722"), option_right=OptionRight.PUT,
        option_expiry=datetime(2026, 10, 19).date(),
        side=OrderSide.SELL, order_type=order_type, quantity=D(8), limit_price=D("1.22"),
        is_closing=True, status=status, filled_quantity=D(0),
        closed_at=closed_at, broker_order_id=uuid.uuid4().hex,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(order)
    db.flush()
    return acct, order


_FILLED = SimpleNamespace(status=OrderStatus.FILLED, filled_quantity=D(8), filled_avg_price=D("1.22"),
                          filled_at=datetime(2026, 10, 19, 16, 1, 13, tzinfo=timezone.utc))


def test_a_take_profit_recorded_cancelled_that_filled_is_recorded_as_the_fill():
    db = SessionLocal()
    try:
        acct, order = _setup(db)
        adapter = _Adapter(_FILLED)
        assert fills_sync.repair_unrecorded_fills(db, acct, adapter) == 1
        assert (order.status, order.filled_quantity, order.filled_avg_price) == (
            OrderStatus.FILLED, D(8), D("1.22"))
        assert order.closed_at is not None
    finally:
        db.rollback()


def test_a_real_cancel_is_read_once_and_left_alone():
    db = SessionLocal()
    try:
        acct, order = _setup(db)
        adapter = _Adapter(SimpleNamespace(status=OrderStatus.CANCELED, filled_quantity=D(0)))
        fills_sync.repair_unrecorded_fills(db, acct, adapter)
        assert order.status == OrderStatus.CANCELED and order.closed_at is not None
        fills_sync.repair_unrecorded_fills(db, acct, adapter)
        assert adapter.reads == 1
    finally:
        db.rollback()


def test_cancels_the_broker_reported_and_stops_are_not_re_read():
    db = SessionLocal()
    try:
        acct, _ = _setup(db, closed_at=datetime.now(timezone.utc))
        _setup_acct_stop = _setup(db, order_type=OrderType.STOP)
        adapter = _Adapter(_FILLED)
        fills_sync.repair_unrecorded_fills(db, acct, adapter)
        fills_sync.repair_unrecorded_fills(db, _setup_acct_stop[0], adapter)
        assert adapter.reads == 0
    finally:
        db.rollback()


# ── a fill whose re-plan is the same order ───────────────────────────────────
# QA 2026-10-21, QQQ 726C (Srini): 2 held, Trim 1 = 50% → 1 @ 1.13 with a linked
# stop at 0.22, and a plain 0.22 stop on the other 1. Trim 1 filled at 14:02:47;
# re-planned on the 1 left it was "50% of 1 = 1 @ 1.13" — identical — so nothing
# was out of sync and nobody asked. The stop stayed at 0.22 instead of moving to
# break-even, Trim 2 never rested, and the trader sold by hand in the selloff.

def _srini_ts():
    return _ts(discord_trim_profit_gate_pct=D("25"), discord_trim_stop_pct=D("0"), discord_trim_qty_pct=D("50"),
               discord_trim2_profit_gate_pct=D("50"), discord_trim2_stop_pct=D("0"),
               discord_trim2_qty_pct=D("50"), discord_fill_stop_pct=D("-75"))


def _srini(book):
    g = _guard(entry_price=D("0.90"), stop_price=D("0.22"), id=uuid.uuid4())
    out = tp.reconcile(book, g, D(2), _srini_ts(), D("0.95"),
                       place_limit=book.place_limit, place_pair=book.place_pair, cancel=book.cancel,
                       reconcile_stop=lambda: "in sync", refresh=book.refresh, now=NOW)
    assert book.calls == [("pair", D(1), D("1.13"), D("0.22"))], out
    g.stop_order_id = book._new(quantity=D(1), stop_price=D("0.22")).id      # the plain stop on the other 1
    return g


def _pass(book, g, held, mark, now):
    return tp.reconcile(book, g, D(held), _srini_ts(), D(mark),
                        place_limit=book.place_limit, place_pair=book.place_pair, cancel=book.cancel,
                        reconcile_stop=lambda: "in sync", refresh=book.refresh, now=now)


def test_a_fill_whose_re_plan_is_the_same_order_is_still_seen():
    book = _Book()
    g = _srini(book)
    t1 = g.tp_order_id
    book.fills_at_broker(t1, "1.13")
    out = _pass(book, g, 1, "1.14", NOW + timedelta(minutes=10))
    assert ("read", t1) in book.calls, "1 held, but Trim 1 and the stop cover 2: ask"
    assert g.sell_count == 1 and "trim 1 filled" in out
    assert g.stop_price == D("0.90"), "Trim 1's stop 0%: break-even on what is left"


def test_then_trim_two_rests_for_the_last_contract():
    book = _Book()
    g = _srini(book)
    book.fills_at_broker(g.tp_order_id, "1.13")
    _pass(book, g, 1, "1.14", NOW + timedelta(minutes=10))
    _pass(book, g, 1, "1.14", NOW + timedelta(minutes=10, seconds=5))
    assert book.calls[-1] == ("pair", D(1), D("1.35"), D("0.90"))


def test_while_everything_is_held_nothing_is_read():
    book = _Book()
    g = _srini(book)
    _pass(book, g, 2, "1.00", NOW + timedelta(seconds=5))
    assert not [c for c in book.calls if c[0] == "read"]


def test_a_mismatch_the_broker_keeps_calling_working_is_asked_about_at_most_every_15s():
    book = _Book()
    g = _srini(book)
    t0 = NOW + timedelta(minutes=10)
    for s in (0, 5, 10, 16):
        _pass(book, g, 1, "1.00", t0 + timedelta(seconds=s))
    reads = [c for c in book.calls if c == ("read", g.tp_order_id)]
    assert len(reads) == 2, reads
