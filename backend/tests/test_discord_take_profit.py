"""Take-profit orders: each trim rests at the broker instead of being watched.

    entry fills      take-profit for Trim 1 (+ the On Fill stop)
    Trim 1 fills     stop moves to Trim 1's level; take-profit for Trim 2
    …

A take-profit and a stop cannot both cover the same contracts as two ordinary
orders — each reserves them. So the take-profit covers the trim's SLICE with a
stop LINKED to it (a broker pair: one fills, the other cancels), and the plain
ladder stop covers the rest.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import app.api.discord_sources as ds
import app.services.discord_take_profit as tp
from app.models.order import InstrumentType, OptionRight, OrderSide, OrderStatus, OrderType
from app.brokers import BrokerOrderRequest
from app.schemas.discord import DiscordSettingsIn
from app.services import (
    discord_auto_trim, discord_channel_settings as dcs, discord_stop_orders, discord_trailing_stop,
)

NOW = datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc)


def _ts(**kw):
    base = dict(
        discord_trim_profit_gate_pct=D("20"), discord_trim_stop_pct=D("-25"), discord_trim_qty_pct=D("50"),
        discord_trim2_profit_gate_pct=D("40"), discord_trim2_stop_pct=D("0"), discord_trim2_qty_pct=D("50"),
        discord_trim3_profit_gate_pct=D("60"), discord_trim3_stop_pct=D("10"), discord_trim3_qty_pct=D("100"),
        discord_trim_count=3, discord_extra_trims=[], discord_fill_stop_pct=None,
        discord_trim_price_threshold=D("0.90"), discord_trim_trail_amount=D("0.25"),
        discord_auto_trim=False, discord_manual_exit=False, discord_tp_orders=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _guard(**kw):
    base = dict(
        symbol="SPY", closed_at=None, entry_price=D("1.00"), sell_count=0, stop_price=None,
        trail_qty=None, trail_amount=None, peak_price=None, stop_order_id=None,
        tp_order_id=None, tp_stop_order_id=None, tp_rung=None, tp_qty=None, tp_off=False,
        tp_backoff_until=None, user_id=uuid.uuid4(),
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── the exit mode ────────────────────────────────────────────────────────────

def test_take_profit_orders_is_its_own_exit_mode():
    ts = _ts(discord_tp_orders=False)
    ds._apply_settings(ts, DiscordSettingsIn(exit_mode="orders"), SimpleNamespace(id=uuid.uuid4()))
    assert (ts.discord_tp_orders, ts.discord_auto_trim, ts.discord_manual_exit) == (True, False, False)
    assert dcs.exit_mode(ts) == "orders"
    ds._apply_settings(ts, DiscordSettingsIn(exit_mode="auto"), SimpleNamespace(id=uuid.uuid4()))
    assert (ts.discord_tp_orders, dcs.exit_mode(ts)) == (False, "auto")


def test_manual_wins_over_take_profit_orders():
    ts = _ts(discord_manual_exit=True)
    assert dcs.exit_mode(ts) == "manual" and tp.enabled(ts) is False


def test_only_active_where_the_broker_links_a_take_profit_to_a_stop():
    assert tp.active(_ts(), SimpleNamespace(supports_exit_pair=True)) is True
    assert tp.active(_ts(), SimpleNamespace(supports_exit_pair=False)) is False
    assert tp.active(_ts(discord_tp_orders=False), SimpleNamespace(supports_exit_pair=True)) is False


def test_without_a_linked_pair_the_mode_runs_as_auto_trim():
    """Alpaca: the trim still fires at its target, by the price sweep."""
    g = _guard()
    assert discord_auto_trim.due_rung(_ts(), g, D("1.25")) == 1


# ── what should rest ─────────────────────────────────────────────────────────

def test_the_first_take_profit_is_trim_ones_slice_at_its_target():
    p = tp.plan(_guard(), D(4), _ts(), D("1.05"))
    assert p == tp.Plan(rung=1, quantity=D(2), price=D("1.20"), stop_price=None)


def test_with_a_stop_level_the_slice_carries_a_linked_stop():
    p = tp.plan(_guard(stop_price=D("0.75")), D(4), _ts(), D("1.05"))
    assert (p.quantity, p.price, p.stop_price) == (D(2), D("1.20"), D("0.75"))


def test_after_trim_one_it_is_trim_twos_slice_of_what_is_left():
    p = tp.plan(_guard(sell_count=1, stop_price=D("0.75")), D(2), _ts(), D("1.25"))
    assert (p.rung, p.quantity, p.price) == (2, D(1), D("1.40"))


def test_the_last_trim_takes_everything_left_under_one_linked_stop():
    p = tp.plan(_guard(sell_count=2, stop_price=D("1.00")), D(1), _ts(), D("1.45"))
    assert (p.rung, p.quantity, p.price, p.stop_price) == (3, D(1), D("1.60"), D("1.00"))


def test_the_target_rounds_up_to_the_cent():
    assert tp.target_price(D("1.02"), D("20")) == D("1.23")          # 1.224
    assert tp.target_price(D("0.55"), D("35")) == D("0.75")          # 0.7425


@pytest.mark.parametrize("guard, held, ts, mark", [
    (_guard(sell_count=3), D(2), _ts(), D("2.00")),                                  # ladder spent: runners
    (_guard(), D(4), _ts(discord_trim_profit_gate_pct=D("0")), D("1.05")),          # no target: waits for an alert
    (_guard(tp_off=True), D(4), _ts(), D("1.05")),                                   # the trader cancelled it
    (_guard(), D(0), _ts(), D("1.05")),                                              # entry not filled yet
    (_guard(entry_price=None), D(4), _ts(), D("1.05")),
    (_guard(stop_price=D("0.75")), D(4), _ts(), D("0.70")),                          # through the stop already
    (_guard(sell_count=1), D(1), _ts(discord_trim_count=2), D("1.30")),              # last trim 50% of 1: rounds to 0
])
def test_nothing_rests_when(guard, held, ts, mark):
    assert tp.plan(guard, held, ts, mark) is None


def test_at_the_target_already_it_goes_out_as_a_plain_limit():
    """A linked take-profit has to sit above the market; a plain limit at the
    target fills at once."""
    p = tp.plan(_guard(stop_price=D("0.75")), D(4), _ts(), D("1.30"))
    assert (p.price, p.stop_price) == (D("1.20"), None)


# ── a fake broker book ───────────────────────────────────────────────────────

class _Book:
    """Order rows by id, and what was asked of the broker."""

    def __init__(self):
        self.orders, self.calls = {}, []

    def get(self, _model, oid):
        return self.orders.get(oid)

    def _new(self, **kw):
        o = SimpleNamespace(**{**dict(id=uuid.uuid4(), status=OrderStatus.SUBMITTED, filled_quantity=D(0),
                                      filled_avg_price=None, limit_price=None, stop_price=None), **kw})
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

    def fill(self, oid, price):
        o = self.orders[oid]
        o.status, o.filled_quantity, o.filled_avg_price = OrderStatus.FILLED, o.quantity, D(price)


def _run(book, guard, held, ts=None, mark="1.05", stop="in sync", **kw):
    seen = {}

    def _stop():
        seen["earmark"] = guard.tp_qty          # what the ladder stop is sized around
        return stop

    out = tp.reconcile(book, guard, D(held), ts or _ts(), D(mark) if mark else None,
                       place_limit=book.place_limit, place_pair=book.place_pair, cancel=book.cancel,
                       reconcile_stop=_stop, now=NOW, **kw)
    return out, seen.get("earmark")


def test_on_fill_with_no_stop_a_lone_take_profit_rests():
    book, g = _Book(), _guard()
    out, earmark = _run(book, g, 4)
    assert book.calls == [("limit", D(2), D("1.20"))]
    assert (g.tp_rung, g.tp_qty, g.tp_stop_order_id) == (1, D(2), None) and g.tp_order_id is not None
    assert earmark == D(2) and "placed trim 1" in out


def test_with_an_on_fill_stop_the_slice_gets_the_pair_and_the_stop_shrinks_first():
    book, g = _Book(), _guard(stop_price=D("0.75"))
    out, earmark = _run(book, g, 4)
    assert book.calls == [("pair", D(2), D("1.20"), D("0.75"))]
    assert earmark == D(2)                       # the ladder stop was sized to 4 - 2 BEFORE the pair went out
    assert discord_stop_orders.desired_quantity(D(4), g) == D(2)
    assert g.tp_stop_order_id is not None


def test_a_second_pass_changes_nothing():
    book, g = _Book(), _guard(stop_price=D("0.75"))
    _run(book, g, 4)
    out, _ = _run(book, g, 4)
    assert len(book.calls) == 1 and out.startswith("in sync")


def test_trim_one_filling_moves_the_stop_and_rests_trim_two():
    book, g = _Book(), _guard(stop_price=D("0.70"))          # On Fill stop at -30%
    _run(book, g, 4)
    book.fill(g.tp_order_id, "1.20")
    out, _ = _run(book, g, 2, mark="1.22")                    # 2 left after the trim
    assert g.sell_count == 1 and g.stop_price == D("0.75")    # Trim 1's stop: -25%
    pair = [c for c in book.calls if c[0] == "pair"][-1]
    assert pair == ("pair", D(1), D("1.40"), D("0.75"))       # Trim 2: half of 2, at +40%, same stop level
    assert g.tp_rung == 2 and out == "trim 1 filled; placed trim 2: 1 @ 1.40"


def test_the_whole_ladder_walks_down_to_flat():
    book, g = _Book(), _guard(stop_price=D("0.70"))
    _run(book, g, 4)
    book.fill(g.tp_order_id, "1.20"); _run(book, g, 2, mark="1.22")
    book.fill(g.tp_order_id, "1.40"); _run(book, g, 1, mark="1.42")
    assert (g.sell_count, g.stop_price, g.tp_rung, g.tp_qty) == (2, D("1.00"), 3, D(1))   # break-even after Trim 2
    assert [c for c in book.calls if c[0] == "pair"][-1] == ("pair", D(1), D("1.60"), D("1.00"))
    assert discord_stop_orders.desired_quantity(D(1), g) == D(0)      # last trim: the pair's stop is the only stop
    book.fill(g.tp_order_id, "1.60"); _run(book, g, 0, mark=None)
    assert g.sell_count == 3 and g.tp_order_id is None and g.tp_qty is None


def test_the_linked_stop_is_cancelled_when_its_take_profit_fills():
    """The broker does this itself; our row must not be left 'working'."""
    book, g = _Book(), _guard(stop_price=D("0.75"))
    _run(book, g, 4)
    sl = g.tp_stop_order_id
    book.fill(g.tp_order_id, "1.20")
    _run(book, g, 2, mark="1.22")
    assert book.orders[sl].status == OrderStatus.CANCELED


def test_a_slice_that_is_stopped_out_does_not_advance_the_ladder():
    book, g = _Book(), _guard(stop_price=D("0.75"))
    _run(book, g, 4)
    tp_id, sl_id = g.tp_order_id, g.tp_stop_order_id
    book.fill(sl_id, "0.74")
    out, _ = _run(book, g, 0, mark=None)
    assert g.sell_count == 0 and g.tp_order_id is None
    assert book.orders[tp_id].status == OrderStatus.CANCELED and out == "stopped out"


def test_a_take_profit_the_trader_cancels_stays_cancelled():
    book, g = _Book(), _guard(stop_price=D("0.75"))
    _run(book, g, 4)
    sl = g.tp_stop_order_id
    book.orders[g.tp_order_id].status = OrderStatus.CANCELED       # cancelled in Order History
    out, earmark = _run(book, g, 4)
    assert g.tp_off is True and out == "removed by the trader"
    assert book.orders[sl].status == OrderStatus.CANCELED          # its stop goes too…
    assert earmark is None                                         # …and the ladder stop covers everything again
    assert not [c for c in book.calls[1:] if c[0] in ("pair", "limit")]


def test_a_day_order_that_expired_overnight_is_placed_again_next_session():
    book, g = _Book(), _guard(stop_price=D("0.75"))
    _run(book, g, 4)
    book.orders[g.tp_order_id].status = OrderStatus.CANCELED       # how an expiry can read
    out, _ = _run(book, g, 4, in_session=False)
    assert g.tp_off is False and g.tp_order_id is None             # not a trader's cancel
    assert len([c for c in book.calls if c[0] == "pair"]) == 1     # nothing new while closed
    _run(book, g, 4)
    assert len([c for c in book.calls if c[0] == "pair"]) == 2


def test_yesterdays_order_seen_cancelled_this_morning_is_an_expiry_not_a_cancel():
    """The app was down overnight: the DAY order's expiry is first seen in the
    next session. It must not read as the trader cancelling it."""
    book, g = _Book(), _guard(stop_price=D("0.75"))
    _run(book, g, 4)
    o = book.orders[g.tp_order_id]
    o.status, o.submitted_at = OrderStatus.CANCELED, datetime.now(timezone.utc) - timedelta(days=1)
    _run(book, g, 4)                                               # in session
    assert g.tp_off is False
    assert len([c for c in book.calls if c[0] == "pair"]) == 2     # placed again


def test_a_refused_take_profit_backs_off_and_frees_the_earmark():
    book, g = _Book(), _guard(stop_price=D("0.75"))

    def _refuse(*a):
        raise RuntimeError("OPENAPI_PARAM_ERR")

    book.place_pair = _refuse
    out, _ = _run(book, g, 4)
    assert out == "refused — backing off" and g.tp_qty is None and g.tp_order_id is None
    assert g.tp_backoff_until == NOW + tp.BACKOFF
    book.place_pair = lambda *a: pytest.fail("must wait out the backoff")
    assert _run(book, g, 4)[0] == "backing off"


def test_an_add_that_changes_the_size_replaces_the_order():
    book, g = _Book(), _guard(stop_price=D("0.75"))
    _run(book, g, 4)
    first_tp, first_sl = g.tp_order_id, g.tp_stop_order_id
    _run(book, g, 8)                                               # the position doubled
    assert book.orders[first_tp].status == book.orders[first_sl].status == OrderStatus.CANCELED
    assert [c for c in book.calls if c[0] == "pair"][-1] == ("pair", D(4), D("1.20"), D("0.75"))
    assert g.tp_off is False                                       # our own cancel, not the trader's


def test_moving_the_stop_level_replaces_the_pair():
    book, g = _Book(), _guard(stop_price=D("0.75"))
    _run(book, g, 4)
    g.stop_price = D("0.90")                                       # set by hand from Positions
    _run(book, g, 4)
    assert [c for c in book.calls if c[0] == "pair"][-1] == ("pair", D(2), D("1.20"), D("0.90"))


def test_nothing_is_placed_while_the_stop_is_closing_the_position():
    book, g = _Book(), _guard(stop_price=D("0.75"))
    out, _ = _run(book, g, 4, stop="closed (stop refused)")
    assert book.calls == [] and g.tp_qty is None and "being closed" in out


def test_release_frees_the_contracts_without_reading_as_a_trader_cancel():
    book, g = _Book(), _guard(stop_price=D("0.75"))
    _run(book, g, 4)
    tp_id, sl_id = g.tp_order_id, g.tp_stop_order_id
    assert tp.release(book, g, book.cancel) is True
    assert book.orders[tp_id].status == book.orders[sl_id].status == OrderStatus.CANCELED
    assert (g.tp_order_id, g.tp_stop_order_id, g.tp_qty, g.tp_off) == (None, None, None, False)
    _run(book, g, 4)                                               # the next pass puts it back
    assert g.tp_order_id is not None


# ── the neighbours ───────────────────────────────────────────────────────────

def test_the_emulated_stop_stands_down_while_a_linked_stop_rests():
    g = _guard(stop_price=D("0.75"), tp_stop_order_id=uuid.uuid4())
    assert discord_trailing_stop.decide(g, D("0.70"), D(1)) is None
    g.tp_stop_order_id = None
    assert discord_trailing_stop.decide(g, D("0.70"), D(1))[0] == discord_trailing_stop.STOP


def test_the_ladder_stop_is_sized_around_the_take_profit():
    assert discord_stop_orders.desired_quantity(D(4), _guard(tp_qty=D(2))) == D(2)
    assert discord_stop_orders.desired_quantity(D(4), _guard()) == D(4)
    assert discord_stop_orders.desired_quantity(D(4), _guard(tp_qty=D(2), trail_qty=D(1))) == D(1)


# ── Webull's pair ────────────────────────────────────────────────────────────

def test_webull_sends_the_pair_as_one_linked_day_combo(monkeypatch):
    from app.brokers.webull import WebullAdapter

    sent = {}

    class _OrderV2:
        def place_option(self, account_id, orders, client_combo_order_id=None):
            sent.update(account=account_id, orders=orders, combo=client_combo_order_id)
            return SimpleNamespace(status_code=200, json=lambda: {})

    a = WebullAdapter({"app_key": "k", "app_secret": "s", "account_id": "ACC", "paper": True})
    monkeypatch.setattr(a, "_trade_client", lambda: SimpleNamespace(order_v2=_OrderV2()))

    def _req(otype, **kw):
        return BrokerOrderRequest(
            instrument_type=InstrumentType.OPTION, symbol="SPY", side=OrderSide.SELL, order_type=otype,
            quantity=D(2), option_expiry=NOW.date(), option_strike=D(770), option_right=OptionRight.CALL,
            client_order_id=str(uuid.uuid4()), is_closing=True, **kw)

    r_tp, r_sl = a.place_exit_pair(_req(OrderType.LIMIT, limit_price=D("1.20")),
                                   _req(OrderType.STOP, stop_price=D("0.75")))
    tp_leg, sl_leg = sent["orders"]
    assert (tp_leg["combo_type"], sl_leg["combo_type"]) == ("STOP_PROFIT", "STOP_LOSS")
    assert tp_leg["time_in_force"] == sl_leg["time_in_force"] == "DAY"     # GTC is refused for the combo
    assert (tp_leg["order_type"], D(tp_leg["limit_price"])) == ("LIMIT", D("1.20"))
    assert (sl_leg["order_type"], D(sl_leg["stop_price"])) == ("STOP_LOSS", D("0.75"))
    assert tp_leg["position_intent"] == sl_leg["position_intent"] == "SELL_TO_CLOSE"
    assert sent["combo"] and tp_leg["client_order_id"] != sl_leg["client_order_id"]
    assert (r_tp.broker_order_id, r_sl.broker_order_id) == (tp_leg["client_order_id"], sl_leg["client_order_id"])
    assert WebullAdapter.supports_exit_pair is True


# ── placing the pair: two rows, one broker call ──────────────────────────────

def _pair_placer(monkeypatch, adapter):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.models.broker_account import BrokerAccount
    from app.models.order import Fill, Order
    from app.models.user import User
    from app.services import audit, events, order_intent, pnl_poller

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (User, BrokerAccount, Order, Fill):
        m.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    monkeypatch.setattr(audit, "record", lambda *a, **k: None)
    monkeypatch.setattr(events, "publish", lambda *a, **k: None)
    marked = []
    monkeypatch.setattr(order_intent, "mark_app_originated", lambda oid: marked.append(oid))
    acct = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    guard = SimpleNamespace(symbol="spy", option_strike=D("770"), option_right="call", option_expiry=NOW.date())
    return db, Order, marked, pnl_poller._make_pair_placer(db, acct, acct, adapter, guard)


def test_the_pair_is_recorded_as_two_closing_orders_from_one_call(monkeypatch):
    calls = []

    def _place(tp_req, sl_req):
        calls.append((tp_req, sl_req))
        return tuple(SimpleNamespace(broker_order_id=r.client_order_id, status=OrderStatus.SUBMITTED,
                                     submitted_at=NOW) for r in (tp_req, sl_req))

    db, Order, marked, place = _pair_placer(monkeypatch, SimpleNamespace(place_exit_pair=_place))
    tp_id, sl_id = place(D(2), D("1.20"), D("0.757"))
    tp_row, sl_row = db.get(Order, tp_id), db.get(Order, sl_id)
    assert len(calls) == 1 and set(marked) == {tp_id, sl_id}       # the listener is told both are ours
    assert (tp_row.order_type, tp_row.limit_price, tp_row.quantity) == (OrderType.LIMIT, D("1.20"), D(2))
    assert (sl_row.order_type, sl_row.stop_price, sl_row.quantity) == (OrderType.STOP, D("0.75"), D(2))   # rounded DOWN
    assert tp_row.is_closing and sl_row.is_closing and tp_row.symbol == "SPY"
    assert tp_row.status == sl_row.status == OrderStatus.SUBMITTED
    assert (calls[0][0].is_closing, calls[0][0].side, calls[0][1].stop_price) == (True, OrderSide.SELL, D("0.75"))


def test_a_refused_pair_leaves_no_rejected_stop_behind(monkeypatch):
    """A REJECTED STOP row reads to the stop reconciler as "the broker refuses
    stops on this contract" — and its answer to that is to close the position."""
    def _refuse(tp_req, sl_req):
        raise RuntimeError("OPENAPI_TRADE_STOP_PROFIT_PRICE_GT_OPENPRICE")

    db, Order, _marked, place = _pair_placer(monkeypatch, SimpleNamespace(place_exit_pair=_refuse))
    with pytest.raises(RuntimeError):
        place(D(2), D("1.20"), D("0.75"))
    rows = db.query(Order).all()
    assert [(r.order_type, r.status) for r in rows] == [(OrderType.LIMIT, OrderStatus.REJECTED)]
    assert "STOP_PROFIT_PRICE" in rows[0].reject_reason
