"""Rejected-trade WhatsApp messages (REJECTED_TRADE_WHATSAPP_ENABLED): the same
rejection pipeline as the texts, a separate row per order, its own opt-in with
a consent time, the approved template's variables, and SMS left exactly as it
was. Twilio is never called: httpx is refused and sends go through fakes.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
from fastapi import HTTPException

from app.config import get_settings
from app.services import notifications, rejected_trade_sms as rts, sms, sms_costs, sms_outbox
from test_rejected_trade_sms import ET_1032, FakeTwilio, flag, no_real_twilio, reject, world  # noqa: F401

WA = rts.WA_CATEGORY


@pytest.fixture
def wa(monkeypatch):
    def set_(on=True):
        monkeypatch.setattr(get_settings(), "rejected_trade_whatsapp_enabled", on)
    set_(False)
    return set_


def opted_in(world, u):
    u.whatsapp_notifications_enabled, u.whatsapp_opt_in_at = True, datetime.now(timezone.utc)
    world.db.commit()
    return u


def by_cat(world, o):
    return {r.category: r for r in world.rows(o)}


def test_off_by_default():
    from app.config import Settings
    assert Settings.model_fields["rejected_trade_whatsapp_enabled"].default is False
    assert Settings.model_fields["twilio_whatsapp_rejected_template_sid"].default == ""


# ── recording: same pipeline, separate row ────────────────────────────────


def test_whatsapp_alone_records_one_whatsapp_row_and_leaves_sms_alone(world, wa):
    wa(True)
    t = world.user(name="Dana")
    o = world.order(t, world.account(t))
    reject(world, o)
    rows = by_cat(world, o)
    assert set(rows) == {WA}                                     # no SMS row while the SMS flag is off
    r = rows[WA]
    assert (r.status, r.dedup_key, r.outcome) == ("pending", f"{WA}:{o.id}", "broker_rejected")
    # the existing texts keep going out exactly as before
    assert notifications._sms_pref_attr("order.rejected") == "sms_on_trade_rejected"   # noqa: SLF001


def test_the_template_variables_carry_the_trade_details(world, wa):
    wa(True)
    t = world.user()
    o = world.order(t, world.account(t))
    reject(world, o)
    v = json.loads(by_cat(world, o)[WA].body)
    ref = str(o.id).replace("-", "")[:8].upper()
    assert v == {"1": "Your BUY 2 TSLA 450C 10/23 order (LIMIT $5.1, Alpaca Paper)",
                 "2": "Rejected by the broker: insufficient buying power",
                 "3": "Oct 23 10:32 AM ET", "4": ref, "5": "https://test.kopyya.com/trades"}


def test_outcome_unknown_is_never_called_rejected(world, wa):
    from app.services.retry_scheduler import NEEDS_REVIEW_REASON
    wa(True)
    t = world.user()
    o = world.order(t, world.account(t), reason=NEEDS_REVIEW_REASON)
    v = rts.whatsapp_variables(world.db, o, "needs_review", ET_1032)
    assert "ejected" not in v["2"] and "may have been placed" in v["2"]


def test_both_channels_make_two_rows_and_the_text_is_unchanged(world, wa):
    wa(True)
    world.flag(True)
    t = world.user()
    o = world.order(t, world.account(t))
    reject(world, o)
    rows = by_cat(world, o)
    assert set(rows) == {rts.CATEGORY, WA}
    assert rows[rts.CATEGORY].body == rts.message_for(world.db, world.db.get(type(o), o.id), "broker_rejected", ET_1032)
    assert rows[rts.CATEGORY].subject_key != rows[WA].subject_key   # separate repeat windows


def test_one_rejection_seen_twice_makes_one_whatsapp_row(world, wa):
    from app.database import SessionLocal
    from app.models.order import Order, OrderStatus
    wa(True)
    t = world.user()
    o = world.order(t, world.account(t), status=OrderStatus.SUBMITTED)
    a, b = SessionLocal(), SessionLocal()
    try:
        oa, ob = a.get(Order, o.id), b.get(Order, o.id)
        oa.status = ob.status = OrderStatus.REJECTED
        a.commit()
        b.commit()
    finally:
        a.close()
        b.close()
    assert [r.category for r in world.rows(o)] == [WA]


def test_blocked_is_kept_but_never_sent(world, wa):
    wa(True)
    t = world.user()
    o = world.order(t, world.account(t))
    reject(world, o, "retry_skipped: daily_loss_limit_hit")
    r = by_cat(world, o)[WA]
    assert (r.status, r.skip_reason) == ("skipped", "not_texted_blocked")


# ── sending ───────────────────────────────────────────────────────────────


def queued_wa(world, wa, *, opt_in=True, sms_flag=False, **user_kw):
    wa(True)
    world.flag(sms_flag)
    t = world.user(**user_kw)
    if opt_in:
        opted_in(world, t)
    o = world.order(t, world.account(t))
    reject(world, o)
    return t, o, by_cat(world, o)


def test_an_opted_in_user_gets_the_template_once_and_no_text(world, wa):
    _, o, rows = queued_wa(world, wa)
    text, whatsapp = FakeTwilio(), FakeTwilio()
    due = rows[WA].next_attempt_at
    sms_outbox.send_due(now=due, send=text, send_whatsapp=whatsapp)
    sms_outbox.send_due(now=due + timedelta(minutes=5), send=text, send_whatsapp=whatsapp)
    assert text.sent == [] and len(whatsapp.sent) == 1
    to, variables = whatsapp.sent[0]
    assert to == "+15555550123" and variables["2"].startswith("Rejected by the broker")
    r = by_cat(world, o)[WA]
    assert (r.status, r.twilio_sid, r.phone_last4) == ("sent", "SM1", "0123")


@pytest.mark.parametrize("opt_in,kw,why", [(False, {}, "whatsapp_not_opted_in"),
                                           (True, {"phone": None}, "no_phone")])
def test_whatsapp_needs_its_own_opt_in(world, wa, opt_in, kw, why):
    # SMS switches on (the world default) don't count for WhatsApp
    _, o, rows = queued_wa(world, wa, opt_in=opt_in, **kw)
    whatsapp = FakeTwilio()
    sms_outbox.send_due(now=rows[WA].next_attempt_at, send=FakeTwilio(), send_whatsapp=whatsapp)
    assert whatsapp.sent == []
    r = by_cat(world, o)[WA]
    assert (r.status, r.skip_reason) == ("skipped", why)


def test_preferences_are_independent_both_ways(world, wa):
    # SMS on + WhatsApp opted out → the text goes, the WhatsApp doesn't
    _, o, rows = queued_wa(world, wa, opt_in=False, sms_flag=True)
    text, whatsapp = FakeTwilio(), FakeTwilio()
    sms_outbox.send_due(now=rows[WA].next_attempt_at, send=text, send_whatsapp=whatsapp)
    assert len(text.sent) == 1 and whatsapp.sent == []
    # WhatsApp opted in + SMS off → only WhatsApp
    _, o2, rows2 = queued_wa(world, wa, sms_flag=True, sms_on=False, phone="+15555550999")
    text2, whatsapp2 = FakeTwilio(), FakeTwilio()
    sms_outbox.send_due(now=rows2[WA].next_attempt_at, send=text2, send_whatsapp=whatsapp2)
    assert text2.sent == [] and len(whatsapp2.sent) == 1


def test_a_sent_whatsapp_never_counts_as_a_recent_text(world, wa):
    _, o, rows = queued_wa(world, wa, sms_flag=True)
    text, whatsapp = FakeTwilio(), FakeTwilio()
    sms_outbox.send_due(now=rows[WA].next_attempt_at, send=text, send_whatsapp=whatsapp)
    assert len(text.sent) == 1 and len(whatsapp.sent) == 1


def test_delivery_status_is_read_back_for_whatsapp(world, wa):
    _, o, rows = queued_wa(world, wa)
    sms_outbox.send_due(now=rows[WA].next_attempt_at, send=FakeTwilio(), send_whatsapp=FakeTwilio())
    later = rows[WA].next_attempt_at + timedelta(minutes=2)
    sms_outbox.check_statuses(now=later, fetch=lambda sid: {"status": "read"})
    assert by_cat(world, o)[WA].status == "delivered"


# ── Twilio request ────────────────────────────────────────────────────────


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
    monkeypatch.setattr(s, "twilio_whatsapp_from", "whatsapp:+15550001111")
    monkeypatch.setattr(s, "twilio_whatsapp_rejected_template_sid", "HXtemplate")


def test_whatsapp_is_sent_as_the_approved_template(creds, monkeypatch):
    seen = {}

    def post(url, data, **k):
        seen.update(data)
        return Resp(201, {"sid": "SMwa", "status": "queued"})
    monkeypatch.setattr(sms.httpx, "post", post)
    r = sms.send_whatsapp_detailed("+15555550123", {"1": "x"})
    assert r.ok and r.sid == "SMwa"
    assert seen == {"To": "whatsapp:+15555550123", "From": "whatsapp:+15550001111", "ContentSid": "HXtemplate",
                    "ContentVariables": '{"1": "x"}'}


def test_no_template_means_no_request(creds, monkeypatch):
    monkeypatch.setattr(get_settings(), "twilio_whatsapp_rejected_template_sid", "")
    r = sms.send_whatsapp_detailed("+15555550123", {"1": "x"})    # httpx.post would raise (no_real_twilio)
    assert not r.ok and r.error == "whatsapp not configured"


def test_the_sms_request_is_unchanged(creds, monkeypatch):
    seen = {}

    def post(url, data, **k):
        seen.update(data)
        return Resp(201, {"sid": "SMx", "status": "accepted", "num_segments": "1"})
    monkeypatch.setattr(sms.httpx, "post", post)
    assert sms.send_sms_detailed("+15555550123", "hello").ok
    assert seen == {"To": "+15555550123", "Body": sms.compose("hello"), "MessagingServiceSid": "MGtest"}


# ── opt-in on the profile ─────────────────────────────────────────────────


def _patch(world, u, **fields):
    from app.api.auth import update_me
    from app.schemas.auth import UpdateMeIn
    return update_me(UpdateMeIn(**fields), db=world.db, user=u)


def test_opting_in_records_consent_and_opting_out_clears_it(world):
    u = world.user()
    _patch(world, u, whatsapp_notifications_enabled=True)
    assert u.whatsapp_notifications_enabled and u.whatsapp_opt_in_at is not None
    assert u.sms_notifications_enabled is True                       # SMS untouched
    _patch(world, u, whatsapp_notifications_enabled=False)
    assert not u.whatsapp_notifications_enabled and u.whatsapp_opt_in_at is None


def test_opting_in_needs_a_phone(world):
    u = world.user(phone=None)
    with pytest.raises(HTTPException) as e:
        _patch(world, u, whatsapp_notifications_enabled=True)
    assert e.value.status_code == 422


def test_a_new_number_needs_a_new_opt_in(world):
    u = opted_in(world, world.user())
    _patch(world, u, phone="+15555550888")
    assert not u.whatsapp_notifications_enabled and u.whatsapp_opt_in_at is None


# ── admin views ───────────────────────────────────────────────────────────


def test_admin_rejected_shows_text_and_whatsapp_apart(world, wa):
    from app.api.admin import admin_rejected_orders
    _, o, rows = queued_wa(world, wa, sms_flag=True)
    out = admin_rejected_orders(db=world.db, _=None, role=None, from_=None, to=None, limit=500)
    mine = next(r for r in out["rejections"] if r["order_id"] == str(o.id))
    assert mine["sms"]["status"] == "pending" and mine["whatsapp"]["status"] == "pending"
    assert out["whatsapp_tracking_enabled"] is True


def test_whatsapp_costs_are_reported_apart_from_sms():
    def get(path, params=None):
        cat = (params or {}).get("Category")
        rec = {"channels-whatsapp": {"count": "16", "price": "0.0224", "start_date": "2026-10-01"},
               "channels-messaging": {"count": "3", "price": "0.015"}}.get(cat, {})
        return {"usage_records": [rec]}
    w = sms_costs.whatsapp_usage(get)
    assert w["ThisMonth"]["messages"] == 16 and str(w["ThisMonth"]["cost"]) == "0.0374"


def test_the_message_log_counts_whatsapp_apart():
    page = {"messages": [
        {"direction": "outbound-api", "status": "delivered", "to": "+15555550100", "num_segments": "1"},
        {"direction": "outbound-api", "status": "read", "to": "whatsapp:+15555550100"},
        {"direction": "outbound-api", "status": "undelivered", "to": "whatsapp:+15555550101", "error_code": 63016},
    ], "next_page_uri": None}
    m = sms_costs.twilio_messages(datetime(2026, 10, 23).date(), lambda p, params=None: page)
    assert m["sent"] == 1 and m["status"] == {"delivered": 1}                 # SMS figures unchanged
    assert m["whatsapp_status"] == {"read": 1, "undelivered": 1}
