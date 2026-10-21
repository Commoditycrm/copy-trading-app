"""A mirror whose send timed out may already be live at the broker.

Safety review of PR #467: the retry path re-placed timed-out mirrors blind.
Alpaca/Webull refuse the reused client_order_id, leaving the mirror REJECTED
while the first order works; SnapTrade sends no client id, so the re-place was
a second real order. Now a timeout means "look first".
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.brokers.base import BrokerAdapter, BrokerOrderRequest, BrokerOrderResult
from app.brokers.snaptrade import SnapTradeAdapter
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.settings import RetryInterval
from app.services import order_retry, retry_scheduler
from app.services.order_retry import (
    OUTCOME_UNKNOWN_REASON,
    AdoptedOrderResult,
    OrderOutcomeUnknown,
    is_outcome_unknown_error,
    place_order_with_recovery,
)

NOW = datetime.now(timezone.utc)


class ReadTimeout(Exception):
    pass


class ConnectTimeout(Exception):
    pass


def _req(**kw):
    kw.setdefault("client_order_id", str(uuid.uuid4()))
    return BrokerOrderRequest(
        instrument_type=kw.pop("instrument_type", InstrumentType.STOCK),
        symbol=kw.pop("symbol", "AAPL"),
        side=kw.pop("side", OrderSide.BUY),
        order_type=OrderType.MARKET,
        quantity=kw.pop("quantity", D(3)),
        **kw,
    )


def _result(oid="brk-1", status=OrderStatus.SUBMITTED):
    return BrokerOrderResult(broker_order_id=oid, status=status, submitted_at=NOW)


class FakeAdapter(BrokerAdapter):
    """place_order raises/returns from `outcomes` in turn; find_placed_order
    from `lookups` (an exception instance is raised)."""
    name = "fake"

    def __init__(self, outcomes, lookups=(), *, dedupes=True):
        self.outcomes = list(outcomes)
        self.lookups = list(lookups)
        self.dedupes_client_order_id = dedupes
        self.placed: list[BrokerOrderRequest] = []
        self.looked: list[dict] = []

    def verify_connection(self):  # pragma: no cover
        raise NotImplementedError

    def get_order(self, broker_order_id):  # pragma: no cover
        raise NotImplementedError

    def place_order(self, req):
        self.placed.append(req)
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    def find_placed_order(self, req, *, placed_after, exclude_ids=frozenset()):
        self.looked.append({"req": req, "placed_after": placed_after, "exclude_ids": exclude_ids})
        out = self.lookups.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(order_retry.time, "sleep", lambda s: None)


# ── classification ──────────────────────────────────────────────────────────

def test_read_timeout_and_reset_are_outcome_unknown():
    assert is_outcome_unknown_error(ReadTimeout("Read timed out. (read timeout=10)"))
    assert is_outcome_unknown_error(ConnectionResetError("Connection reset by peer"))
    wrapped = RuntimeError("SnapTrade place_force_order: boom")
    wrapped.__cause__ = ReadTimeout("x")
    assert is_outcome_unknown_error(wrapped)


def test_connect_failures_sent_nothing():
    assert not is_outcome_unknown_error(ConnectTimeout("Connection to host timed out. (connect timeout=5)"))
    assert not is_outcome_unknown_error(RuntimeError("HTTP 429 too many requests"))


# ── client-id brokers (Alpaca / Webull) ─────────────────────────────────────

def test_timeout_then_found_adopts_without_second_place():
    a = FakeAdapter([ReadTimeout("Read timed out.")], [_result("brk-live", OrderStatus.FILLED)])
    req = _req()
    out = place_order_with_recovery(a, req, initial_backoff_s=0)
    assert isinstance(out, AdoptedOrderResult)
    assert out.broker_order_id == "brk-live" and out.status == OrderStatus.FILLED
    assert len(a.placed) == 1


def test_timeout_then_not_found_replaces_once_with_same_client_id():
    a = FakeAdapter([ReadTimeout("Read timed out."), _result("brk-2")], [None])
    req = _req()
    out = place_order_with_recovery(a, req, initial_backoff_s=0)
    assert out.broker_order_id == "brk-2" and not isinstance(out, AdoptedOrderResult)
    assert len(a.placed) == 2
    assert {r.client_order_id for r in a.placed} == {req.client_order_id}


def test_duplicate_client_id_422_adopts():
    dup = Exception('{"code":40010001,"message":"client_order_id must be unique"}')
    a = FakeAdapter([dup], [_result("brk-first")])
    out = place_order_with_recovery(a, _req(), initial_backoff_s=0)
    assert isinstance(out, AdoptedOrderResult) and out.broker_order_id == "brk-first"
    assert len(a.placed) == 1


def test_lagging_lookup_then_duplicate_refusal_still_adopts():
    # Lookup misses (broker not caught up), re-send is refused as a duplicate,
    # second lookup finds the original.
    dup = Exception("client_order_id must be unique")
    a = FakeAdapter([ReadTimeout("timed out"), dup], [None, _result("brk-first")])
    out = place_order_with_recovery(a, _req(), initial_backoff_s=0)
    assert out.broker_order_id == "brk-first"
    assert len(a.placed) == 2


def test_failed_lookup_on_deduping_broker_still_resends():
    a = FakeAdapter([ReadTimeout("timed out"), _result("brk-2")], [RuntimeError("lookup 503")])
    out = place_order_with_recovery(a, _req(), initial_backoff_s=0)
    assert out.broker_order_id == "brk-2"


# ── contract-matched brokers (SnapTrade) ────────────────────────────────────

def test_non_deduping_timeout_not_found_raises_without_resend():
    a = FakeAdapter([ReadTimeout("timed out")], [None], dedupes=False)
    with pytest.raises(OrderOutcomeUnknown):
        place_order_with_recovery(a, _req(), initial_backoff_s=0)
    assert len(a.placed) == 1


class _FakeSnapClient:
    def __init__(self, orders, place_exc):
        self.place_calls = 0
        self._orders = orders
        self._place_exc = place_exc
        self.trading = SimpleNamespace(
            place_force_order=self._place, place_mleg_order=self._place,
        )
        self.account_information = SimpleNamespace(
            get_user_account_orders=lambda **kw: SimpleNamespace(body=list(self._orders)),
        )

    def _place(self, **kw):
        self.place_calls += 1
        raise self._place_exc


def _snap(orders, place_exc=None):
    a = SnapTradeAdapter({"snaptrade_user_id": "u", "snaptrade_user_secret": "s", "account_id": "acc"})
    a._client = _FakeSnapClient(orders, place_exc or ReadTimeout("Read timed out."))
    return a


def _snap_order(oid, *, symbol="AAPL", action="BUY", qty=3, placed=None, option=None):
    o = {
        "brokerage_order_id": oid,
        "status": "EXECUTED",
        "action": action,
        "total_quantity": str(qty),
        "time_placed": (placed or datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        "universal_symbol": {"symbol": symbol, "raw_symbol": symbol, "type": {"code": "cs"}},
    }
    if option:
        o["universal_symbol"] = None
        o["option_symbol"] = option
    return o


def test_snaptrade_timeout_with_matching_recent_order_adopts_and_never_places_twice():
    a = _snap([
        _snap_order("old", placed=datetime.now(timezone.utc) - timedelta(hours=2)),
        _snap_order("other-qty", qty=5),
        _snap_order("live"),
    ])
    out = place_order_with_recovery(a, _req(), initial_backoff_s=0)
    assert isinstance(out, AdoptedOrderResult) and out.broker_order_id == "live"
    assert out.status == OrderStatus.FILLED
    assert a._client.place_calls == 1


def test_snaptrade_lookup_skips_orders_we_already_own():
    a = _snap([_snap_order("sibling")])
    with pytest.raises(OrderOutcomeUnknown):
        place_order_with_recovery(a, _req(), initial_backoff_s=0, exclude_ids=frozenset({"sibling"}))
    assert a._client.place_calls == 1


def test_snaptrade_matches_option_contract():
    exp = date(2026, 11, 20)
    opt = {"option_type": "CALL", "strike_price": 150.0, "expiration_date": exp.isoformat(),
           "underlying_symbol": {"symbol": "AAPL"}, "ticker": "AAPL  261120C00150000"}
    a = _snap([_snap_order("opt", action="BUY_TO_OPEN", qty=2, option=opt)])
    req = _req(instrument_type=InstrumentType.OPTION, quantity=D(2), option_expiry=exp,
               option_strike=D("150"), option_right=OptionRight.CALL)
    assert a.find_placed_order(req, placed_after=NOW).broker_order_id == "opt"
    wrong = _req(instrument_type=InstrumentType.OPTION, quantity=D(2), option_expiry=exp,
                 option_strike=D("155"), option_right=OptionRight.CALL)
    assert a.find_placed_order(wrong, placed_after=NOW) is None


# ── retry_scheduler ─────────────────────────────────────────────────────────

class _Session:
    def __init__(self, objs):
        self._objs = objs
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, model, key):
        return self._objs.get((model, key))

    def execute(self, *a, **k):
        return SimpleNamespace(scalars=lambda: iter(()))

    def commit(self):
        self.commits += 1


def _scheduled_child(reason):
    from app.models.broker_account import BrokerAccount
    from app.models.settings import SubscriberSettings

    parent = Order(id=uuid.uuid4(), user_id=uuid.uuid4(), instrument_type=InstrumentType.STOCK,
                   symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=D(3),
                   status=OrderStatus.FILLED)
    child = Order(id=uuid.uuid4(), user_id=uuid.uuid4(), parent_order_id=parent.id,
                  broker_account_id=uuid.uuid4(), instrument_type=InstrumentType.STOCK,
                  symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=D(3),
                  status=OrderStatus.RETRY_PENDING, retry_count=0, is_closing=False,
                  reject_reason=reason, created_at=NOW - timedelta(minutes=2),
                  filled_quantity=D(0))
    settings = SimpleNamespace(retry_max_attempts=3, retry_interval_open=RetryInterval.ONE_M,
                               retry_interval_close=RetryInterval.ONE_M)
    acct = SimpleNamespace(id=child.broker_account_id, encrypted_credentials="x")
    objs = {(Order, child.id): child, (Order, parent.id): parent,
            (SubscriberSettings, child.user_id): settings, (BrokerAccount, child.broker_account_id): acct}
    return child, objs


@pytest.fixture
def scheduler(monkeypatch):
    audits: list[str] = []

    def run(child, objs, adapter):
        monkeypatch.setattr(retry_scheduler, "SessionLocal", lambda: _Session(objs))
        monkeypatch.setattr(retry_scheduler, "_passes_gates", lambda *a: None)
        monkeypatch.setattr(retry_scheduler, "decrypt_json", lambda c: {})
        monkeypatch.setattr(retry_scheduler, "adapter_for", lambda acct, creds: adapter)
        monkeypatch.setattr(retry_scheduler, "_notify_retry_failed", lambda *a: None)
        monkeypatch.setattr(retry_scheduler.events, "publish", lambda *a: None)
        monkeypatch.setattr(retry_scheduler.audit, "record",
                            lambda db, **kw: audits.append(kw["action"]))
        return retry_scheduler._retry_one_order(child.id)

    run.audits = audits
    return run


def test_scheduler_adopts_found_order_without_placing(scheduler):
    child, objs = _scheduled_child(OUTCOME_UNKNOWN_REASON)
    a = FakeAdapter([], [_result("brk-live", OrderStatus.FILLED)], dedupes=False)
    assert scheduler(child, objs, a) == "adopted"
    assert a.placed == []
    assert child.broker_order_id == "brk-live" and child.status == OrderStatus.FILLED
    assert scheduler.audits == ["copy.retry_adopted"]


def test_scheduler_never_resends_an_unconfirmed_non_deduping_send(scheduler):
    # SnapTrade-style: first send unconfirmed, lookup finds nothing → review,
    # not a re-send (absence on a lagging order list proves nothing).
    child, objs = _scheduled_child(OUTCOME_UNKNOWN_REASON)
    a = FakeAdapter([], [None], dedupes=False)
    assert scheduler(child, objs, a) == "needs_review"
    assert a.placed == []
    assert child.status == OrderStatus.REJECTED
    assert child.reject_reason == retry_scheduler.NEEDS_REVIEW_REASON
    assert scheduler.audits == ["copy.retry_needs_review"]


def test_scheduler_reviews_when_lookup_keeps_failing(scheduler):
    child, objs = _scheduled_child(OUTCOME_UNKNOWN_REASON)
    child.retry_count = 2                       # last of 3 attempts
    a = FakeAdapter([], [RuntimeError("list orders 503")], dedupes=False)
    assert scheduler(child, objs, a) == "needs_review"
    assert a.placed == []
    assert scheduler.audits == ["copy.retry_needs_review"]


def test_scheduler_send_that_times_out_is_only_looked_up_next_time(scheduler):
    # After a 429 the scheduler sends; that send times out unconfirmed. It's
    # parked for a lookup-only re-check, which finds nothing → review. One send total.
    child, objs = _scheduled_child("transient broker error, will retry")
    a = FakeAdapter([ReadTimeout("timed out")], [None, None], dedupes=False)
    assert scheduler(child, objs, a) == "rescheduled"
    assert child.reject_reason == OUTCOME_UNKNOWN_REASON
    child.status = OrderStatus.RETRY_PENDING
    assert scheduler(child, objs, a) == "needs_review"
    assert len(a.placed) == 1


def test_scheduler_client_id_broker_resends_when_absent(scheduler):
    child, objs = _scheduled_child(OUTCOME_UNKNOWN_REASON)
    a = FakeAdapter([_result("brk-2")], [None], dedupes=True)
    assert scheduler(child, objs, a) == "succeeded"
    assert [r.client_order_id for r in a.placed] == [str(child.id)]


def test_scheduler_holds_when_lookup_cannot_run(scheduler):
    child, objs = _scheduled_child(OUTCOME_UNKNOWN_REASON)
    a = FakeAdapter([], [RuntimeError("list orders 503")], dedupes=False)
    assert scheduler(child, objs, a) == "rescheduled"
    assert a.placed == []
    assert child.status == OrderStatus.RETRY_PENDING
    assert child.reject_reason == OUTCOME_UNKNOWN_REASON


def test_scheduler_skips_contract_lookup_after_plain_transient(scheduler):
    # A 429 placed nothing — don't spend a list call or risk a lookalike match.
    child, objs = _scheduled_child("transient broker error, will retry")
    a = FakeAdapter([_result("brk-new")], [], dedupes=False)
    assert scheduler(child, objs, a) == "succeeded"
    assert a.looked == [] and len(a.placed) == 1


def test_scheduler_checks_client_id_broker_before_resend(scheduler):
    child, objs = _scheduled_child("transient broker error, will retry")
    a = FakeAdapter([], [_result("brk-first")], dedupes=True)
    assert scheduler(child, objs, a) == "adopted"
    assert a.placed == []
    assert a.looked[0]["req"].client_order_id == str(child.id)


# ── Alpaca adapter lookup ───────────────────────────────────────────────────

def _alpaca(get_by_client_id):
    from app.brokers.alpaca import AlpacaAdapter

    a = AlpacaAdapter({"api_key": "k", "api_secret": "s"})
    a._client = SimpleNamespace(get_order_by_client_id=get_by_client_id)
    return a


def test_alpaca_lookup_by_client_id():
    seen = []
    order = SimpleNamespace(id="alp-1", status=None, submitted_at=NOW, filled_qty="0", filled_avg_price=None)
    a = _alpaca(lambda cid: seen.append(cid) or order)
    req = _req()
    assert a.find_placed_order(req, placed_after=NOW).broker_order_id == "alp-1"
    assert seen == [req.client_order_id]


def test_alpaca_lookup_404_is_absent_other_errors_raise():
    from alpaca.common.exceptions import APIError

    def missing(cid):
        raise APIError('{"code":40410000,"message":"order not found"}')

    def broken(cid):
        raise APIError('{"code":50010000,"message":"internal server error"}')

    assert _alpaca(missing).find_placed_order(_req(), placed_after=NOW) is None
    with pytest.raises(APIError):
        _alpaca(broken).find_placed_order(_req(), placed_after=NOW)
