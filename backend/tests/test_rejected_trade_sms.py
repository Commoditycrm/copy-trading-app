"""Rejected-trade texts (REJECTED_TRADE_SMS_ENABLED): what each rejection path
is classified as, how the text reads, that one rejection seen by several paths
is texted once, that retry loops don't text every attempt, that opt-in is
respected, that Twilio trouble is retried and never reaches trading, and that
with the flag off nothing changes.

Uses the database at DATABASE_URL (like test_rejection_alerts). Twilio is never
called: httpx is replaced for the whole module and sends go through a fake.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.sms_notification import SmsNotification
from app.models.user import User, UserRole
from app.services import notifications, rejected_trade_sms as rts, rejection_alerts, sms, sms_outbox
from app.services.order_retry import OUTCOME_UNKNOWN_REASON
from app.services.retry_scheduler import NEEDS_REVIEW_REASON

ET_1032 = datetime(2026, 10, 23, 14, 32, tzinfo=timezone.utc)      # 10:32 AM ET


@pytest.fixture(autouse=True)
def no_real_twilio(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to reach Twilio")
    monkeypatch.setattr(sms.httpx, "post", refuse)
    monkeypatch.setattr(sms.httpx, "get", refuse)


@pytest.fixture
def flag(monkeypatch):
    def set_(on=True, label="QA"):
        monkeypatch.setattr(get_settings(), "rejected_trade_sms_enabled", on)
        monkeypatch.setattr(get_settings(), "sms_environment_label", label)
        monkeypatch.setattr(get_settings(), "frontend_base_url", "https://test.kopyya.com")
    set_(False, "")
    return set_


@pytest.fixture
def world(flag):
    rejection_alerts.install()
    db = SessionLocal()
    made: list = []

    def user(role=UserRole.TRADER, phone="+15555550123", sms_on=True, rejected_on=True, name=None):
        u = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:12]}@test.local", password_hash="x", role=role,
                 display_name=name, phone=phone, sms_notifications_enabled=sms_on, sms_on_trade_rejected=rejected_on)
        db.add(u)
        db.flush()
        made.append(u)
        return u

    def account(u, broker=BrokerName.ALPACA, paper=True, label="My account U1234567"):
        a = BrokerAccount(id=uuid.uuid4(), user_id=u.id, broker=broker, label=label, is_paper=paper,
                          encrypted_credentials="x")
        db.add(a)
        db.flush()
        return a

    def order(u, acct, status=OrderStatus.PENDING, *, option=True, side=OrderSide.BUY, qty="2",
              otype=OrderType.LIMIT, limit="5.10", reason=None, parent=None, symbol="TSLA", commit=True):
        o = Order(id=uuid.uuid4(), user_id=u.id, broker_account_id=acct.id, parent_order_id=parent,
                  instrument_type=InstrumentType.OPTION if option else InstrumentType.STOCK,
                  symbol=symbol, side=side, order_type=otype, quantity=Decimal(qty),
                  limit_price=Decimal(limit) if limit else None,
                  option_expiry=date(2026, 10, 23) if option else None,
                  option_strike=Decimal("450") if option else None,
                  option_right=OptionRight.CALL if option else None,
                  status=status, reject_reason=reason)
        db.add(o)
        if commit:
            db.commit()
        return o

    def rows(o=None, u=None):
        db.expire_all()
        q = select(SmsNotification)
        q = q.where(SmsNotification.order_id == o.id) if o is not None else q.where(SmsNotification.user_id == u.id)
        return list(db.execute(q).scalars())

    yield SimpleNamespace(db=db, user=user, account=account, order=order, rows=rows, flag=flag)
    db.rollback()
    for u in made:
        for r in db.execute(select(SmsNotification).where(SmsNotification.user_id == u.id)).scalars():
            db.delete(r)
        for o in db.query(Order).filter(Order.user_id == u.id).all():
            db.delete(o)
        for a in db.query(BrokerAccount).filter(BrokerAccount.user_id == u.id).all():
            db.delete(a)
    db.flush()
    for u in made:
        persisted = db.get(User, u.id)
        if persisted is not None:
            db.delete(persisted)
    db.commit()
    db.close()


def reject(world, o, reason="broker_error: insufficient buying power"):
    o.status = OrderStatus.REJECTED
    o.reject_reason = reason
    o.closed_at = ET_1032
    world.db.commit()


# ── classification: every REJECTED path ───────────────────────────────────


def _o(reason, **kw):
    return Order(reject_reason=reason, **kw)


@pytest.mark.parametrize("reason,previous,outcome", [
    # placement refusals: _place_trader_order, copy_engine user-fixable, pnl_poller TP, background closes
    ("broker_error: HTTP Status: 417, Code: OPENAPI_PARAM_ERR, Msg: Parameter error", OrderStatus.PENDING, "broker_rejected"),
    ("insufficient buying power", OrderStatus.PENDING, "broker_rejected"),
    ("auto_liquidate_error: market closed", OrderStatus.PENDING, "broker_rejected"),
    ("position_take_profit_error: rejected", OrderStatus.PENDING, "broker_rejected"),
    ("trader_sl_monitor_error: rejected", OrderStatus.PENDING, "broker_rejected"),
    ("bracket_exit_error: rejected", OrderStatus.PENDING, "broker_rejected"),
    # synchronous REJECTED from the adapter / listener inserts (no reason)
    (None, OrderStatus.PENDING, "broker_rejected"),
    (None, None, "broker_rejected"),
    # accepted, then rejected (stream / poll / fills_sync)
    (None, OrderStatus.SUBMITTED, "broker_rejected"),
    (None, OrderStatus.ACCEPTED, "broker_rejected"),
    (None, OrderStatus.PARTIALLY_FILLED, "broker_rejected"),
    # our side couldn't place it
    ("Broker credentials error: InvalidToken", OrderStatus.PENDING, "failed"),
    ("Broker credentials error: InvalidToken", OrderStatus.RETRY_PENDING, "failed"),
    ("recovery_failed: connection reset", OrderStatus.PENDING, "failed"),
    ("still 503 from broker", OrderStatus.RETRY_PENDING, "failed"),           # retries exhausted
    # outcome unknown — may have been placed
    (NEEDS_REVIEW_REASON, OrderStatus.RETRY_PENDING, "needs_review"),
    ("The broker didn't confirm this order (timed out) — it may still have been placed. Check your broker account.",
     OrderStatus.PENDING, "needs_review"),
    (OUTCOME_UNKNOWN_REASON, OrderStatus.RETRY_PENDING, "needs_review"),
    # Kopyya safeguards
    ("retry_skipped: daily_loss_limit_hit", OrderStatus.RETRY_PENDING, "blocked"),
    ("retry_skipped: copy_disabled", OrderStatus.RETRY_PENDING, "blocked"),
    ("Skipped: this sell would open a short position — you don't hold it", OrderStatus.PENDING, "blocked"),
    # housekeeping
    ("parent_order_missing", OrderStatus.RETRY_PENDING, "internal"),
    ("broker_account_missing", OrderStatus.PENDING, "internal"),
])
def test_every_rejection_path_is_classified(reason, previous, outcome):
    assert rts.classify(_o(reason), previous) == outcome


def test_only_real_failures_are_texted():
    assert set(rts.TEXTED) == {"broker_rejected", "failed", "needs_review"}


# ── wording ───────────────────────────────────────────────────────────────


def test_the_text_carries_the_trade_details(world):
    world.flag(True, "QA")
    t = world.user(name="Dana")
    o = world.order(t, world.account(t), reason="insufficient buying power")
    o.closed_at = ET_1032
    body = rts.message_for(world.db, o, "broker_rejected", ET_1032)
    ref = str(o.id).replace("-", "")[:8].upper()
    # the shape of the A2P sample: "Your BUY TSLA order was rejected: <reason>. View: <link>"
    assert body == ("Kopyya QA: Your BUY 2 TSLA 450C 10/23 order (LIMIT $5.1, Alpaca Paper) was rejected: "
                    f"insufficient buying power. Oct 23 10:32 AM ET, Ref {ref}. View: https://test.kopyya.com/trades")
    final = sms.compose(body)
    assert final.startswith("Kopyya QA: Your BUY") and final.count("Kopyya") == 1
    assert final.endswith("Reply STOP to opt out.") and "\n" not in final


def test_a_copy_names_the_trader_and_a_stock_reads_in_shares(world):
    t = world.user(name="Trader Joe")
    s = world.user(role=UserRole.SUBSCRIBER)
    parent = world.order(t, world.account(t), option=False, qty="10", otype=OrderType.MARKET, limit=None,
                         status=OrderStatus.FILLED)
    child = world.order(s, world.account(s, BrokerName.WEBULL, paper=False), option=False, qty="1",
                        otype=OrderType.MARKET, limit=None, parent=parent.id, side=OrderSide.SELL)
    body = rts.message_for(world.db, child, "broker_rejected", ET_1032)
    assert body.startswith("Kopyya: Your copy of Trader Joe's SELL 1 TSLA order (MARKET, Webull) was rejected:")


def test_outcome_unknown_is_never_called_rejected(world):
    t = world.user()
    o = world.order(t, world.account(t), reason=NEEDS_REVIEW_REASON)
    body = rts.message_for(world.db, o, "needs_review", ET_1032)
    assert "Rejected" not in body and "rejected" not in body
    assert "was not confirmed by the broker and may have been placed" in body


@pytest.mark.parametrize("raw,clean", [
    ("broker_error: HTTP Status: 417, Code: OPENAPI_PARAM_ERR, Msg: Parameter error, invalid market,symbol, "
     "RequestID: f01d3c65-8ac6-4da9-80a4-0e453bfbd321", "Parameter error, invalid market,symbol (OPENAPI_PARAM_ERR)"),
    ("HTTP Status: 417, Code: OPENAPI_DUPLICATED_TASK_REQUEST, Msg: Request is being processed, RequestID: 6f25",
     "Request is being processed (OPENAPI_DUPLICATED_TASK_REQUEST)"),
    ("account U1234567 has insufficient buying power", "account **** has insufficient buying power"),
    ("position_stop_loss_error: broker_error: order would trigger PDT", "order would trigger PDT"),
    ("Broker credentials error: Fernet InvalidToken at /app/x.py", "Broker login problem; reconnect your broker in Kopyya."),
    ("", "No reason given by the broker"),
])
def test_reasons_are_tidied_and_never_leak_account_numbers(raw, clean):
    assert rts.clean_reason(raw, "broker_rejected") == clean


def test_the_worst_case_text_stays_gsm7_and_within_two_segments(world):
    """An emoji in a display name would switch the text to UCS-2 (67 chars a
    segment); a long broker reason would run to three segments."""
    t = world.user(name="Jayyyyyyyy🐔 “The Sniper” — Options Desk Main")
    s = world.user(role=UserRole.SUBSCRIBER)
    parent = world.order(t, world.account(t), status=OrderStatus.FILLED)
    child = world.order(s, world.account(s, BrokerName.SNAPTRADE, paper=False), parent=parent.id,
                        otype=OrderType.STOP_LIMIT, qty="100", reason="x" * 400)
    child.stop_price = Decimal("12.50")
    final = sms.compose(rts.message_for(world.db, child, "broker_rejected", ET_1032))
    assert len(final) <= rts.MAX_CHARS
    assert set(final) <= rts._GSM7                                       # noqa: SLF001
    assert "was rejected: xxx" in final and "xxx..." in final and "...." not in final
    assert final.endswith("Reply STOP to opt out.")
    assert "Your copy of Jayyyyyyyy \"The Sniper\" - Opti's BUY" in final   # emoji gone, then 30 chars


@pytest.mark.parametrize("outcome,verb", [("failed", "could not be placed: "),
                                           ("blocked", "was not placed: "), ("internal", "was not placed: ")])
def test_other_outcomes_say_what_happened(world, outcome, verb):
    t = world.user()
    o = world.order(t, world.account(t), reason="retry_skipped: daily_loss_limit_hit")
    body = rts.message_for(world.db, o, outcome, ET_1032)
    assert verb in body and "rejected" not in body


def test_the_flag_and_label_default_off_and_blank():
    from app.config import Settings
    assert Settings.model_fields["rejected_trade_sms_enabled"].default is False
    assert Settings.model_fields["sms_environment_label"].default == ""


def test_the_broker_line_never_shows_the_account_label_or_number(world):
    t = world.user()
    acct = world.account(t, BrokerName.ALPACA, paper=False, label="Joint U99887766")
    assert rts.broker_label(acct) == "Alpaca"


# ── the hook: once per order, from any path ───────────────────────────────


def test_flag_off_records_nothing_and_existing_texts_are_unchanged(world):
    t = world.user()
    o = world.order(t, world.account(t))
    reject(world, o)
    assert world.rows(o) == []
    assert notifications._sms_pref_attr("order.rejected") == "sms_on_trade_rejected"   # noqa: SLF001
    assert notifications._sms_pref_attr("copy.rejected") == "sms_on_trade_rejected"    # noqa: SLF001


def test_flag_on_moves_rejection_texts_to_the_central_path(world):
    world.flag(True)
    assert notifications._sms_pref_attr("order.rejected") is None          # noqa: SLF001
    assert notifications._sms_pref_attr("copy.rejected") is None           # noqa: SLF001
    # other categories keep texting exactly as before
    assert notifications._sms_pref_attr("trader.order_rejected") == "sms_on_trade_rejected"  # noqa: SLF001
    assert notifications._sms_pref_attr("broker.disconnected") == "sms_on_broker_connection"  # noqa: SLF001


@pytest.mark.parametrize("before", [OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.RETRY_PENDING])
def test_a_rejection_from_any_status_is_queued_once_after_the_commit(world, before):
    world.flag(True)
    t = world.user()
    o = world.order(t, world.account(t), status=before)
    reject(world, o)
    [row] = world.rows(o)
    assert row.status == "pending" and row.dedup_key == f"trade_rejected:{o.id}"
    assert row.next_attempt_at >= row.created_at + rts.SEND_DELAY - timedelta(seconds=1)
    assert row.outcome == ("failed" if before == OrderStatus.RETRY_PENDING else "broker_rejected")


def test_an_order_inserted_already_rejected_is_queued(world):
    world.flag(True)
    t = world.user()
    o = world.order(t, world.account(t), status=OrderStatus.REJECTED, reason=None)
    assert len(world.rows(o)) == 1


def test_the_same_rejection_seen_by_two_paths_makes_one_row(world):
    """The stream and the poll each load the order as SUBMITTED in their own
    session and both commit REJECTED."""
    world.flag(True)
    t = world.user()
    o = world.order(t, world.account(t), status=OrderStatus.SUBMITTED)
    a, b = SessionLocal(), SessionLocal()
    try:
        oa, ob = a.get(Order, o.id), b.get(Order, o.id)
        oa.status = OrderStatus.REJECTED
        ob.status = OrderStatus.REJECTED
        a.commit()
        b.commit()
    finally:
        a.close()
        b.close()
    assert len(world.rows(o)) == 1
    o2 = world.db.get(Order, o.id)
    o2.reject_reason = "seen again by fills_sync"
    world.db.commit()
    assert len(world.rows(o)) == 1


@pytest.mark.parametrize("status,reason", [
    (OrderStatus.RETRY_PENDING, "transient broker error, will retry (1/3)"),
    (OrderStatus.RETRY_PENDING, OUTCOME_UNKNOWN_REASON),
])
def test_retry_pending_and_outcome_unknown_are_not_texted(world, status, reason):
    world.flag(True)
    t = world.user()
    o = world.order(t, world.account(t))
    o.status, o.reject_reason = status, reason
    world.db.commit()
    assert world.rows(o) == []


def test_a_rolled_back_rejection_is_not_queued(world):
    world.flag(True)
    t = world.user()
    o = world.order(t, world.account(t))
    o.status = OrderStatus.REJECTED
    world.db.flush()
    world.db.rollback()
    assert world.rows(o) == []


def test_blocked_and_internal_are_kept_but_never_sent(world):
    world.flag(True)
    t = world.user()
    acct = world.account(t)
    blocked = world.order(t, acct, status=OrderStatus.RETRY_PENDING)
    reject(world, blocked, "retry_skipped: daily_loss_limit_hit")
    internal = world.order(t, acct, status=OrderStatus.RETRY_PENDING, symbol="AAPL")
    reject(world, internal, "parent_order_missing")
    assert [(r.status, r.skip_reason) for r in world.rows(blocked)] == [("skipped", "not_texted_blocked")]
    assert [(r.status, r.skip_reason) for r in world.rows(internal)] == [("skipped", "not_texted_internal")]


def test_a_failure_to_queue_never_breaks_the_trade_commit(world, monkeypatch):
    world.flag(True)
    monkeypatch.setattr(rts, "queue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    t = world.user()
    o = world.order(t, world.account(t))
    reject(world, o)                                     # no exception
    world.db.expire_all()
    assert world.db.get(Order, o.id).status == OrderStatus.REJECTED


# ── the outbox: delay, opt-in, retries, repeats ───────────────────────────


class FakeTwilio:
    def __init__(self, *results):
        self.results, self.sent = list(results), []

    def __call__(self, to, body):
        self.sent.append((to, body))
        return self.results.pop(0) if self.results else sms.SmsResult(ok=True, sid=f"SM{len(self.sent)}", status="queued",
                                                                    num_segments=2)


def queued(world, **user_kw):
    world.flag(True)
    t = world.user(**user_kw)
    o = world.order(t, world.account(t))
    reject(world, o)
    [row] = world.rows(o)
    return t, o, row


def test_a_text_waits_for_the_delay_then_goes_once(world):
    _, o, row = queued(world)
    tw = FakeTwilio()
    sms_outbox.send_due(now=row.next_attempt_at - timedelta(seconds=1), send=tw)
    assert tw.sent == []
    sms_outbox.send_due(now=row.next_attempt_at, send=tw)
    sms_outbox.send_due(now=row.next_attempt_at + timedelta(minutes=5), send=tw)
    assert len(tw.sent) == 1 and tw.sent[0][0] == "+15555550123"
    [row] = world.rows(o)
    assert (row.status, row.twilio_sid, row.attempts, row.phone_last4, row.num_segments) == ("sent", "SM1", 1, "0123", 2)


def test_two_sender_loops_at_once_send_a_text_once(world):
    """SKIP LOCKED: overlapping passes (a restart while one runs) never both
    send the same row."""
    import threading
    _, o, row = queued(world)
    gate = threading.Barrier(2)
    sent = []

    def slow_send(to, body):
        sent.append(to)
        return sms.SmsResult(ok=True, sid="SMx", status="queued")

    def run():
        gate.wait()
        sms_outbox.send_due(now=row.next_attempt_at, send=slow_send)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(sent) == 1
    assert world.rows(o)[0].status == "sent"


@pytest.mark.parametrize("kw,why", [
    ({"phone": None}, "no_phone"),
    ({"sms_on": False}, "sms_disabled"),
    ({"rejected_on": False}, "category_disabled"),
])
def test_opt_in_is_respected_at_send_time(world, kw, why):
    _, o, row = queued(world, **kw)
    tw = FakeTwilio()
    sms_outbox.send_due(now=row.next_attempt_at, send=tw)
    assert tw.sent == []
    assert [(r.status, r.skip_reason) for r in world.rows(o)] == [("skipped", why)]


def test_transient_twilio_errors_back_off_then_give_up(world):
    _, o, row = queued(world)
    busy = sms.SmsResult(ok=False, error="HTTP 503", transient=True)
    tw = FakeTwilio(*([busy] * sms_outbox.MAX_ATTEMPTS))
    now = row.next_attempt_at
    for _ in range(sms_outbox.MAX_ATTEMPTS):
        sms_outbox.send_due(now=now, send=tw)
        [r] = world.rows(o)
        if r.status != "pending":
            break
        assert r.next_attempt_at > now
        now = r.next_attempt_at
    [r] = world.rows(o)
    assert (r.status, r.attempts, r.last_error) == ("failed", sms_outbox.MAX_ATTEMPTS, "HTTP 503")


def test_twilio_refusing_the_message_is_final(world):
    _, o, row = queued(world)
    tw = FakeTwilio(sms.SmsResult(ok=False, error="Invalid 'To' number", error_code=21211))
    sms_outbox.send_due(now=row.next_attempt_at, send=tw)
    [r] = world.rows(o)
    assert (r.status, r.twilio_error_code, r.attempts) == ("failed", 21211, 1)


def test_a_crashing_sender_is_contained(world):
    _, o, row = queued(world)

    def boom(to, body):
        raise RuntimeError("socket")
    sms_outbox.send_due(now=row.next_attempt_at, send=boom)
    [r] = world.rows(o)
    assert r.status == "pending" and r.attempts == 1


def test_a_retry_loop_rejecting_every_pass_texts_once(world):
    """position_enforcer / take-profit backoff: a new rejected order each pass."""
    world.flag(True)
    t = world.user()
    acct = world.account(t)
    first = world.order(t, acct, side=OrderSide.SELL)
    reject(world, first, "position_stop_loss_error: rejected")
    tw = FakeTwilio()
    [r1] = world.rows(first)
    sms_outbox.send_due(now=r1.next_attempt_at, send=tw)
    second = world.order(t, acct, side=OrderSide.SELL)
    reject(world, second, "position_stop_loss_error: rejected")
    [r2] = world.rows(second)
    sms_outbox.send_due(now=r2.next_attempt_at, send=tw)
    assert len(tw.sent) == 1
    assert world.rows(second)[0].skip_reason == "repeat_within_15m"


def test_a_fallback_that_went_through_cancels_the_text(world):
    """positions.py: the market close is refused, the limit retry is accepted."""
    world.flag(True)
    t = world.user()
    acct = world.account(t)
    market = world.order(t, acct, side=OrderSide.SELL, otype=OrderType.MARKET, limit=None)
    reject(world, market, "market orders not allowed now")
    world.order(t, acct, side=OrderSide.SELL, otype=OrderType.LIMIT, status=OrderStatus.SUBMITTED)
    [r] = world.rows(market)
    tw = FakeTwilio()
    sms_outbox.send_due(now=r.next_attempt_at, send=tw)
    assert tw.sent == [] and world.rows(market)[0].skip_reason == "superseded_by_later_attempt"


def test_a_resting_stop_does_not_cancel_a_refused_take_profit(world):
    """The Srini case: the take-profit sell was refused, then the stop was
    put back. The take-profit still didn't happen, so it's still texted."""
    world.flag(True)
    t = world.user(role=UserRole.SUBSCRIBER)
    acct = world.account(t, BrokerName.WEBULL, paper=False)
    tp = world.order(t, acct, side=OrderSide.SELL, qty="1", limit="1.72")
    reject(world, tp, "broker_error: HTTP Status: 417, Code: OPENAPI_DUPLICATED_TASK_REQUEST, "
                      "Msg: Request is being processed, RequestID: 6f258209")
    world.order(t, acct, side=OrderSide.SELL, qty="1", otype=OrderType.STOP, limit=None,
                status=OrderStatus.SUBMITTED)
    [r] = world.rows(tp)
    tw = FakeTwilio()
    sms_outbox.send_due(now=r.next_attempt_at, send=tw)
    assert len(tw.sent) == 1
    assert "Request is being processed (OPENAPI_DUPLICATED_TASK_REQUEST)" in tw.sent[0][1]
    assert "6f258209" not in tw.sent[0][1]


def test_an_order_adopted_before_the_text_goes_is_not_texted(world):
    _, o, row = queued(world)
    fresh = world.db.get(Order, o.id)
    fresh.status = OrderStatus.SUBMITTED
    world.db.commit()
    tw = FakeTwilio()
    sms_outbox.send_due(now=row.next_attempt_at, send=tw)
    assert tw.sent == [] and world.rows(o)[0].skip_reason == "no_longer_rejected"


# ── Twilio delivery status ────────────────────────────────────────────────


@pytest.mark.parametrize("twilio,final", [("delivered", "delivered"), ("undelivered", "undelivered"),
                                          ("failed", "failed"), ("sent", "sent")])
def test_delivery_status_is_read_back(world, twilio, final):
    _, o, row = queued(world)
    sms_outbox.send_due(now=row.next_attempt_at, send=FakeTwilio())
    later = row.next_attempt_at + timedelta(minutes=2)
    sms_outbox.check_statuses(now=later, fetch=lambda sid: {
        "status": twilio, "price": "-0.00790", "price_unit": "USD", "num_segments": "2",
        "error_code": 30003 if twilio == "undelivered" else None,
        "error_message": "Unreachable handset" if twilio == "undelivered" else None})
    [r] = world.rows(o)
    assert (r.status, r.twilio_status) == (final, twilio)
    assert r.price == Decimal("0.00790") and r.price_unit == "USD"
    if twilio == "undelivered":
        assert r.twilio_error_code == 30003 and r.last_error == "Unreachable handset"


# ── Twilio client ─────────────────────────────────────────────────────────


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.text = code, body, str(body)

    def json(self):
        return self._body


@pytest.fixture
def creds(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(s, "twilio_auth_token", "tok")
    monkeypatch.setattr(s, "twilio_messaging_service_sid", "MGtest")


@pytest.mark.parametrize("resp,ok,transient", [
    (Resp(201, {"sid": "SMx", "status": "accepted", "num_segments": "1"}), True, False),
    (Resp(429, {"code": 20429, "message": "Too Many Requests"}), False, True),
    (Resp(503, {}), False, True),
    (Resp(400, {"code": 21211, "message": "Invalid 'To' Phone Number"}), False, False),
])
def test_send_sms_detailed_reports_what_twilio_said(creds, monkeypatch, resp, ok, transient):
    monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: resp)
    r = sms.send_sms_detailed("+15555550123", "x")
    assert (r.ok, r.transient) == (ok, transient)
    assert sms.send_sms("+15555550123", "x") is ok              # the old bool API is unchanged
    if ok:
        assert (r.sid, r.status, r.num_segments) == ("SMx", "accepted", 1)


def test_a_network_error_is_transient_and_never_raises(creds, monkeypatch):
    def down(*a, **k):
        raise OSError("connection reset")
    monkeypatch.setattr(sms.httpx, "post", down)
    r = sms.send_sms_detailed("+15555550123", "x")
    assert not r.ok and r.transient


@pytest.mark.parametrize("body,expected_start", [
    ("Kopyya QA: Trade Rejected", "Kopyya QA: Trade Rejected"),
    ("Kopyya: hello", "Kopyya: hello"),
    ("hello", "Kopyya: hello"),
])
def test_compose_brands_once(body, expected_start):
    out = sms.compose(body)
    assert out.startswith(expected_start) and out.count("Kopyya") == 1


# ── admin view ────────────────────────────────────────────────────────────


def test_admin_rejected_shows_the_text_status(world):
    from app.api.admin import admin_rejected_orders
    _, o, row = queued(world)
    sms_outbox.send_due(now=row.next_attempt_at, send=FakeTwilio())
    world.db.expire_all()                                # the outbox wrote in its own session
    out = admin_rejected_orders(db=world.db, _=None, role=None, from_=None, to=None, limit=500)
    mine = next(r for r in out["rejections"] if r["order_id"] == str(o.id))
    assert out["sms_tracking_enabled"] is True
    assert mine["sms"]["status"] == "sent" and mine["sms"]["outcome"] == "broker_rejected"
    assert mine["sms"]["phone_last4"] == "0123"
    assert "+1555" not in str(mine["sms"])                         # never the full number
