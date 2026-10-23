"""What went wrong along the way is recorded in the position's summary.

The trader's ask (2026-10-23): capture every action on a position — a sell or
a read refused, a stop move refused, and the retries — in its summary, to
understand what is going on.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.position_event import PositionEvent
from app.models.user import User, UserRole
from app.services import position_events as pe
from app.services import position_history, webull_usage

EXP = date(2026, 10, 23)


# ── helpers ──────────────────────────────────────────────────────────────────

def test_a_holding_is_read_from_its_broker_symbol():
    uid = uuid.uuid4()
    assert pe.position_from_broker_symbol(uid, "QQQ261023C00716000") == (uid, "QQQ", D("716"), "call", EXP)
    assert pe.position_from_broker_symbol(uid, "SPY261023P00765500") == (uid, "SPY", D("765.5"), "put", EXP)
    assert pe.position_from_broker_symbol(uid, "nvda") == (uid, "NVDA", None, None, None)


def test_a_webull_refusal_reads_as_a_person_would():
    raw = ("HTTP Status: 417, Code: OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION, Msg: This order cannot be "
           "entered because it will reverse an existing position., RequestID: 1c51")
    assert pe.short_reason(raw) == ("This order cannot be entered because it will reverse an existing "
                                    "position (OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION)")


def test_a_positions_route_names_its_position():
    uid = uuid.uuid4()
    seen = []

    @pe.tagged("closed by you on Positions")
    def close(broker_symbol, user):
        seen.append(pe.current_position())

    close(broker_symbol="QQQ261023C00716000", user=SimpleNamespace(id=uid))
    assert seen == [(uid, "QQQ", D("716"), "call", EXP)]
    assert pe.current_position() is None


# ── recorded, and once ───────────────────────────────────────────────────────

@pytest.fixture
def world():
    db = SessionLocal()
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local",
                password_hash="x", role=UserRole.TRADER)
    db.add(user)
    db.flush()
    acct = BrokerAccount(id=uuid.uuid4(), user_id=user.id, broker=BrokerName.FAKE, label="t",
                         encrypted_credentials="x")
    db.add(acct)
    db.commit()
    yield SimpleNamespace(db=db, user=user, acct=acct,
                          pos=(user.id, "QQQ", D("716"), "call", EXP))
    db.rollback()
    db.query(PositionEvent).filter(PositionEvent.user_id == user.id).delete()
    db.query(Order).filter(Order.user_id == user.id).delete()
    db.delete(db.get(BrokerAccount, acct.id))
    db.delete(db.get(User, user.id))
    db.commit()
    db.close()


def _events(w):
    w.db.expire_all()
    return [(e.kind, e.note) for e in w.db.query(PositionEvent).filter(PositionEvent.user_id == w.user.id)]


def test_an_issue_is_recorded_once_while_it_repeats(world):
    for _ in range(3):
        pe.record_issue("waiting", "Take-profit left as it is: the broker could not be read.", position=world.pos)
    pe.record_issue("refused", "Webull refused the stop at 0.40.", position=world.pos)
    assert _events(world) == [("waiting", "Take-profit left as it is: the broker could not be read."),
                              ("refused", "Webull refused the stop at 0.40.")]


def test_the_position_being_worked_on_is_used_when_none_is_given(world):
    with pe.about(*world.pos):
        pe.record_issue("retried", "Webull refused the positions read (rate limit) — retried after 1.5s.")
    assert _events(world) == [("retried", "Webull refused the positions read (rate limit) — retried after 1.5s.")]


def test_nothing_is_recorded_with_no_position():
    pe.record_issue("refused", "no position anywhere")       # no error, nothing to attach it to


# ── in the summary ───────────────────────────────────────────────────────────

def _order(w, **kw):
    base = dict(id=uuid.uuid4(), user_id=w.user.id, broker_account_id=w.acct.id,
                instrument_type=InstrumentType.OPTION, symbol="QQQ", option_strike=D("716"),
                option_right=OptionRight.CALL, option_expiry=EXP, side=OrderSide.BUY,
                order_type=OrderType.MARKET, quantity=D(9), status=OrderStatus.FILLED,
                filled_quantity=D(9), filled_avg_price=D("1.03"),
                created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                closed_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    base.update(kw)
    o = Order(**base)
    w.db.add(o)
    w.db.commit()
    return o


def test_the_summary_shows_refusals_and_why_an_order_was_refused(world):
    _order(world)
    _order(world, side=OrderSide.SELL, order_type=OrderType.STOP, stop_price=D("0.40"), quantity=D(9),
           status=OrderStatus.REJECTED, filled_quantity=D(0), filled_avg_price=None,
           created_at=datetime.now(timezone.utc) - timedelta(minutes=4),
           reject_reason="HTTP Status: 417, Code: OPENAPI_X, Msg: Stop price is above the market., RequestID: 9")
    pe.record_issue("rate_limited", "Webull refused a positions read (rate limit, 429), with no wait.",
                    position=world.pos)
    items = position_history.timeline(world.db, world.user.id, world.acct.id, "QQQ",
                                      strike=D("716"), right="call", expiry=EXP)
    refused_stop = next(i for i in items if i["type"] == "order" and i["status"] == "rejected")
    assert "refused by the broker: Stop price is above the market (OPENAPI_X)" in refused_stop["note"]
    issue = next(i for i in items if i["type"] == "event")
    assert (issue["label"], issue["tone"]) == ("Rate-limited", "bad")
    assert "positions read" in issue["note"] and issue["detail"] == ""


# ── a 429 while acting on a position ─────────────────────────────────────────

def test_a_429_while_acting_on_a_position_is_noted_there(monkeypatch):
    notes = []
    monkeypatch.setattr(pe, "record_issue", lambda kind, note, **k: notes.append((kind, note)))
    pos = (uuid.uuid4(), "QQQ", D("716"), "call", EXP)
    with pe.about(*pos):
        webull_usage._note_on_position("/openapi/assets/positions", 0.9)
    webull_usage._note_on_position("/openapi/assets/positions", 0.0)     # no position: nothing
    assert len(notes) == 1 and notes[0][0] == "rate_limited"
    assert "positions read" in notes[0][1] and "after waiting 0.9s" in notes[0][1]


class _Redis:
    def __init__(self):
        self.h, self.z, self.lists = {}, {}, {}

    def hincrby(self, k, f, n):
        self.h.setdefault(k, {})
        self.h[k][f] = self.h[k].get(f, 0) + n

    def hgetall(self, k):
        return dict(self.h.get(k, {}))

    def expire(self, k, s):
        pass

    def zadd(self, k, mapping):
        self.z.setdefault(k, {}).update(mapping)

    def zremrangebyscore(self, k, lo, hi):
        self.z[k] = {m: s for m, s in self.z.get(k, {}).items() if not lo <= s <= hi}

    def zcount(self, k, lo, hi):
        return sum(1 for s in self.z.get(k, {}).values() if lo <= s <= hi)

    def lpush(self, k, v):
        self.lists.setdefault(k, []).insert(0, v)

    def ltrim(self, k, a, b):
        self.lists[k] = self.lists.get(k, [])[a:b + 1]

    def lrange(self, k, a, b):
        return self.lists.get(k, [])[a:b + 1]


def test_each_refusal_is_kept_with_what_explains_it(monkeypatch):
    fake = _Redis()
    monkeypatch.setattr(webull_usage, "_redis", lambda: fake)
    for _ in range(3):
        webull_usage.record("KEY", "/openapi/assets/positions", 200, 5, caller="P&L poller")
    webull_usage.record("KEY", "/openapi/assets/positions", 429, 5, caller="POST /api/positions/{id}/close",
                        waited=1.1)
    s = webull_usage.summary(["KEY"], minutes=1440)
    assert s["rate_limited"] == 1
    r = s["refusals"][0]
    assert (r["caller"], r["endpoint"], r["waited_s"], r["queue_full"]) == (
        "POST /api/positions/{id}/close", "/openapi/assets/positions", 1.1, False)
    assert r["calls_2s_before"] == 3, "three positions reads just before it: too many at once"


# ── the ladder's own refusals and waits ──────────────────────────────────────

def test_a_refused_take_profit_is_noted_with_when_it_tries_again(monkeypatch):
    import app.services.discord_take_profit as tp
    from test_discord_take_profit import NOW, _Book, _guard, _ts

    notes = []
    monkeypatch.setattr(pe, "record_issue", lambda kind, note, **k: notes.append((kind, note)))
    book, g = _Book(), _guard(stop_price=D("0.75"))

    def _refuse(*a):
        raise RuntimeError("HTTP Status: 417, Code: OPENAPI_PARAM_ERR, Msg: Price is invalid., RequestID: 1")

    book.place_pair = _refuse
    tp.reconcile(book, g, D(4), _ts(), D("1.05"), place_limit=book.place_limit, place_pair=book.place_pair,
                 cancel=book.cancel, reconcile_stop=lambda: "in sync", now=NOW)
    assert notes and notes[0][0] == "refused"
    assert "Trim 1's take-profit (2 @ 1.20)" in notes[0][1] and "Price is invalid (OPENAPI_PARAM_ERR)" in notes[0][1]
    assert "Trying again after" in notes[0][1]


def test_a_stop_backing_off_after_a_refusal_says_so(monkeypatch):
    from app.services import discord_stop_orders as so

    notes = []
    monkeypatch.setattr(pe, "record_issue", lambda kind, note, **k: notes.append((kind, note)))
    monkeypatch.setattr(so, "working_exit", lambda db, g: None)
    monkeypatch.setattr(so, "_recent_rejection_reason", lambda db, g: "rate limit exceeded 429")
    g = SimpleNamespace(user_id=uuid.uuid4(), symbol="QQQ", option_strike=D("716"), option_right="call",
                        option_expiry=EXP, stop_price=D("0.40"), stop_order_id=None, tp_qty=None,
                        trail_qty=None)
    db = SimpleNamespace(get=lambda *a: None)
    out = so.reconcile(db, g, D(9), place_stop=lambda q, p: pytest.fail("must not place"),
                       cancel_stop=lambda oid: None, close_position=lambda q: None)
    assert out == "backing off (recent rejection)"
    assert notes and notes[0][0] == "waiting" and "Waiting before trying again" in notes[0][1]
