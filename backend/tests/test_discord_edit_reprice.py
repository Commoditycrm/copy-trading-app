"""An edited alert moves the order it already placed — and nothing else.

    $SPY 770 CALL 0DTE @0.20      -> placed, resting unfilled
            (edited seconds later)
    $SPY 770 CALL 0DTE @0.15      -> the SAME order moves to 0.15

The refusals matter more than the happy path: an edit that repriced a filled
order, a close, a mirror, or a DIFFERENT contract would each do real damage.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_edit as ed
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderStatus, OrderType

EXP = date(2026, 9, 25)


def _order(**kw):
    base = dict(
        id=uuid.uuid4(), user_id=uuid.uuid4(), parent_order_id=None,
        broker_account_id=uuid.uuid4(), symbol="SPY",
        instrument_type=InstrumentType.OPTION, option_expiry=EXP,
        option_strike=Decimal("770"), option_right=OptionRight.CALL,
        side=OrderSide.BUY, is_closing=False, order_type=OrderType.LIMIT,
        quantity=Decimal(1), filled_quantity=Decimal(0),
        limit_price=Decimal("0.20"), status=OrderStatus.SUBMITTED,
        discord_edit_price=None,
        broker_order_id="brk-1",
        # The rest of what _order_event() reads, so the UI push can
        # serialise this row.
        created_at=None, filled_avg_price=None, reject_reason=None,
        stop_price=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _signal(**kw):
    base = dict(action="BUY", symbol="SPY", asset_type="OPTION", strike="770",
                option_type="CALL", expiration=EXP.isoformat(), limit_price="0.15")
    base.update(kw)
    return base


class _DB:
    def __init__(self, order, acct=True):
        self.order = order
        self._acct = SimpleNamespace(broker="alpaca", encrypted_credentials="x") if acct else None
        self.commits = 0

    def get(self, model, key):
        name = getattr(model, "__name__", "")
        if name == "Order":
            return self.order
        return self._acct

    def commit(self):
        self.commits += 1


@pytest.fixture
def broker(monkeypatch):
    """A replace-capable adapter that records what it was asked to do."""
    moved = []
    import app.brokers as brokers
    import app.services.crypto as crypto
    import app.services.discord_reprice as rp
    monkeypatch.setattr(brokers, "adapter_for",
                        lambda a, c: SimpleNamespace(supports_replace=True))
    monkeypatch.setattr(crypto, "decrypt_json", lambda c: {})
    monkeypatch.setattr(rp, "_replace", lambda ad, o, p: moved.append((o.symbol, p)))
    import app.services.copy_engine as ce
    monkeypatch.setattr(ce, "propagate_modify_to_mirrors", lambda oid: None)
    return moved


def _msg(order, signal):
    return SimpleNamespace(order_id=order.id, parsed_signal=signal,
                           discord_message_id="900")


# ── the happy path ───────────────────────────────────────────────────────────

def test_an_edited_price_moves_the_resting_order(broker):
    o = _order()
    out = ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert broker == [("SPY", Decimal("0.15"))]
    assert o.limit_price == Decimal("0.15")
    assert "0.20 -> 0.15" in out


def test_it_moves_up_as_well_as_down(broker):
    """"Any updated price" — the edit is the author's current instruction,
    whichever way it went."""
    o = _order()
    ed.apply_price_edit(_DB(o), _msg(o, _signal(limit_price="0.25")))
    assert broker == [("SPY", Decimal("0.25"))]


# ── refusals ─────────────────────────────────────────────────────────────────

def test_a_filled_order_is_left_alone(broker):
    """A fill cannot be undone. Repricing here would be placing a second trade
    on top of a position the trader already holds."""
    o = _order(status=OrderStatus.FILLED, filled_quantity=Decimal(1))
    out = ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert broker == []
    assert "already filled" in out


def test_a_partial_fill_is_left_alone(broker):
    """The position is real. Resizing the remainder would move its cost basis
    under a ladder already measuring from the first fill."""
    o = _order(status=OrderStatus.PARTIALLY_FILLED, filled_quantity=Decimal(1),
               quantity=Decimal(4))
    out = ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert broker == []
    assert "partially_filled" in out


def test_a_working_order_that_has_started_filling_is_left_alone(broker):
    """The status guard alone is not enough: brokers report fills against an
    order still marked ACCEPTED, so quantity has to be checked too."""
    o = _order(status=OrderStatus.ACCEPTED, filled_quantity=Decimal(1),
               quantity=Decimal(4))
    out = ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert broker == []
    assert "partially filled" in out


def test_a_cancelled_order_is_not_resurrected(broker):
    o = _order(status=OrderStatus.CANCELED)
    assert "canceled" in ed.apply_price_edit(_DB(o), _msg(o, _signal())).lower()
    assert broker == []


def test_a_close_is_never_repriced(broker):
    """An exit sells what is held; it is not a bid the author can correct."""
    o = _order(is_closing=True, side=OrderSide.SELL)
    out = ed.apply_price_edit(_DB(o), _msg(o, _signal(action="SELL")))
    assert broker == []
    assert "close" in out


def test_a_subscriber_mirror_is_not_repriced_directly(broker):
    """A mirror follows its parent. Moving it here would desync the subscriber
    from the trader — the parent's own edit carries it."""
    o = _order(parent_order_id=uuid.uuid4())
    out = ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert broker == []
    assert "mirror" in out


@pytest.mark.parametrize("changed, why", [
    ({"strike": "775"}, "a different strike"),
    ({"option_type": "PUT"}, "the other side of the chain"),
    ({"symbol": "QQQ"}, "a different underlying"),
    ({"expiration": "2026-10-17"}, "a different expiry"),
    ({"action": "SELL"}, "an exit, not an entry"),
])
def test_a_different_contract_is_never_repriced(broker, changed, why):
    """Not a price correction — a different trade. Repricing our resting order
    to match would silently change what we are buying."""
    o = _order()
    out = ed.apply_price_edit(_DB(o), _msg(o, _signal(**changed)))
    assert broker == [], why
    assert "different contract" in out or "close" in out


def test_an_expiry_merely_left_off_is_still_the_same_contract(broker):
    """"$SPY 770 CALL 0DTE @0.20" edited to "$SPY 770 CALL @0.15" dropped the
    expiry; it did not change it."""
    o = _order()
    ed.apply_price_edit(_DB(o), _msg(o, _signal(expiration=None)))
    assert broker == [("SPY", Decimal("0.15"))]


def test_an_edit_with_no_price_does_nothing(broker):
    """Authors edit alerts to append "filled" or fix a typo far more often
    than to change a price."""
    o = _order()
    out = ed.apply_price_edit(_DB(o), _msg(o, _signal(limit_price=None)))
    assert broker == []
    assert "no price" in out


def test_the_same_price_is_not_re_placed(broker):
    """Cancel-and-replace for an identical price is pure risk: a live order
    gives up its queue position for nothing."""
    o = _order()
    out = ed.apply_price_edit(_DB(o), _msg(o, _signal(limit_price="0.20")))
    assert broker == []
    assert out == "same price"


def test_an_alert_that_never_placed_anything_is_a_no_op(broker):
    msg = SimpleNamespace(order_id=None, parsed_signal=_signal(), discord_message_id="9")
    assert "no order" in ed.apply_price_edit(_DB(_order()), msg)
    assert broker == []


def test_a_broker_without_atomic_replace_declines(monkeypatch):
    """Cancel-then-place has a gap where the trader holds nothing, and a failed
    place leaves the entry gone with nothing in its stead — that happened live
    on Webull. A limit resting at a stale price is the recoverable outcome."""
    import app.brokers as brokers
    import app.services.crypto as crypto
    monkeypatch.setattr(brokers, "adapter_for",
                        lambda a, c: SimpleNamespace(supports_replace=False))
    monkeypatch.setattr(crypto, "decrypt_json", lambda c: {})
    o = _order()
    out = ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert "cannot replace atomically" in out
    assert o.limit_price == Decimal("0.20")


def test_a_failed_replace_leaves_our_row_truthful(monkeypatch, broker):
    """If the broker refused, our limit_price must still say what is actually
    resting — otherwise the ladder measures from a price nobody bid."""
    import app.services.discord_reprice as rp

    def _boom(ad, o, p):
        raise RuntimeError("broker said no")

    monkeypatch.setattr(rp, "_replace", _boom)
    o = _order()
    out = ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert "waiting to apply 0.15" in out
    assert o.limit_price == Decimal("0.20")


def test_a_refused_replace_is_held_for_the_retry(monkeypatch, broker):
    """The live failure: pre-market an Alpaca option rests in `accepted` and
    will not take a PATCH until options route at 09:30. Dropping the edit there
    would ignore the author's correction for the whole pre-market session."""
    import app.services.discord_reprice as rp

    monkeypatch.setattr(rp, "_replace", lambda ad, o, p: (_ for _ in ()).throw(
        RuntimeError("order is not open")))
    o = _order()
    ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert o.discord_edit_price == Decimal("0.15")   # still wanted


def test_a_landed_edit_clears_the_pending_price(broker):
    """Otherwise the retry loop would keep re-placing an order already at the
    right price, giving up its queue position every tick."""
    o = _order()
    ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert o.discord_edit_price is None
    assert o.limit_price == Decimal("0.15")


def test_a_broker_that_can_never_replace_does_not_retry_forever(monkeypatch):
    """Not a transient state — holding the price would spin every tick for the
    life of the order."""
    import app.brokers as brokers
    import app.services.crypto as crypto
    monkeypatch.setattr(brokers, "adapter_for",
                        lambda a, c: SimpleNamespace(supports_replace=False))
    monkeypatch.setattr(crypto, "decrypt_json", lambda c: {})
    o = _order()
    ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert o.discord_edit_price is None


def test_a_refusal_never_records_a_pending_price(broker):
    """A filled order, a close, a mirror or a different contract are decided
    BEFORE anything is recorded — otherwise the retry loop would carry an edit
    we already refused."""
    for order in (_order(status=OrderStatus.FILLED, filled_quantity=Decimal(1)),
                  _order(is_closing=True),
                  _order(parent_order_id=uuid.uuid4())):
        ed.apply_price_edit(_DB(order), _msg(order, _signal()))
        assert getattr(order, "discord_edit_price", None) is None


def test_the_mirrors_are_carried_along(monkeypatch, broker):
    """Subscribers are resting at the price the author withdrew, and the
    replacement is app-originated so no listener will detect it for us."""
    import app.services.copy_engine as ce
    carried = []
    monkeypatch.setattr(ce, "propagate_modify_to_mirrors", carried.append)
    o = _order()
    ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert carried == [o.id]


# ── the UI has to hear about it ──────────────────────────────────────────────

def test_a_landed_edit_is_pushed_to_the_ui(monkeypatch, broker):
    """Without this the row keeps showing the OLD limit until a reload — and a
    stale row is indistinguishable from an edit that never applied."""
    import app.services.events as events
    sent = []
    monkeypatch.setattr(events, "publish", lambda uid, payload: sent.append(payload))
    o = _order()
    ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert [p["type"] for p in sent] == ["order.updated"]
    assert sent[0]["order"]["limit_price"] == "0.15"     # the NEW terms ride along


def test_nothing_is_announced_when_the_price_did_not_move(monkeypatch, broker):
    """An announcement for an order that did not change would flash a row and
    trigger a refetch for nothing."""
    import app.services.events as events
    sent = []
    monkeypatch.setattr(events, "publish", lambda uid, payload: sent.append(payload))
    o = _order()
    ed.apply_price_edit(_DB(o), _msg(o, _signal(limit_price="0.20")))   # same price
    assert sent == []


def test_a_failed_announcement_does_not_undo_the_reprice(monkeypatch, broker):
    """The price is already changed at the broker. An SSE failure must not read
    as a failed reprice."""
    import app.services.events as events
    monkeypatch.setattr(events, "publish", lambda *a: (_ for _ in ()).throw(RuntimeError("bus down")))
    o = _order()
    out = ed.apply_price_edit(_DB(o), _msg(o, _signal()))
    assert out.startswith("repriced")
    assert o.limit_price == Decimal("0.15")
