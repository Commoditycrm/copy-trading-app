"""A re-posted entry from the SAME channel is a correction, not a second trade.

QA 2026-10-06: Clint posted "$SPY 781 CALL 0DTE @0.63, Lotto!" and, 30s later,
the same at @0.56 as a NEW message. A subscriber got two SPY 781C positions.
Two DIFFERENT channels calling the same contract are two signals and both trade.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.discord_account import DiscordAccount
from app.models.discord_alert_source import DiscordAlertSource
from app.models.discord_message import DiscordMessage, DiscordMessageStatus
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import discord_edit, discord_repost

USER = uuid.uuid4()
NOW = datetime(2026, 10, 6, 16, 17, tzinfo=timezone.utc)
EXP = date(2026, 10, 6)


@pytest.fixture
def db():
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.dialects.postgresql import JSONB

    @compiles(JSONB, "sqlite")
    def _jsonb(type_, compiler, **kw):  # noqa: ANN001, ARG001
        return "JSON"

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (User, DiscordAccount, DiscordAlertSource, DiscordMessage, Order):
        m.__table__.create(eng)
    s = sessionmaker(bind=eng)()
    s.add(User(id=USER, email="u@x.com", password_hash="x", role=UserRole.TRADER, is_active=True))
    s.commit()
    return s


def _source(db, label):
    src = DiscordAlertSource(user_id=USER, label=label, channel_id=str(uuid.uuid4().int)[:12], status="connected")
    db.add(src); db.commit()
    return src


def _signal(strike="781", right="call", price="0.56", **kw):
    sig = {"action": "BUY", "symbol": "SPY", "asset_type": "OPTION", "strike": strike,
           "option_type": right, "expiration": EXP.isoformat(), "limit_price": price}
    sig.update(kw)
    return sig


def _entry(db, src, *, ago_s=30, strike="781", status=OrderStatus.FILLED, filled="2",
           otype=OrderType.MARKET, limit=None, signal=None):
    o = Order(id=uuid.uuid4(), user_id=USER, instrument_type=InstrumentType.OPTION, symbol="SPY",
              side=OrderSide.BUY, order_type=otype, quantity=D(2), limit_price=D(limit) if limit else None,
              status=status, filled_quantity=D(filled), filled_avg_price=D("0.64") if D(filled) > 0 else None,
              option_expiry=EXP, option_strike=D(strike), option_right=OptionRight.CALL)
    db.add(o); db.flush()
    m = DiscordMessage(id=uuid.uuid4(), source_id=src.id, user_id=USER, discord_message_id=str(uuid.uuid4().int)[:18],
                       discord_channel_id=src.channel_id, content="$SPY 781 CALL 0DTE @0.63",
                       status=DiscordMessageStatus.ORDER_CREATED, order_id=o.id,
                       parsed_signal=signal or _signal(strike=strike, price="0.63"),
                       created_at=NOW - timedelta(seconds=ago_s))
    db.add(m); db.commit()
    return m, o


def _new_msg(src):
    return SimpleNamespace(id=uuid.uuid4(), source_id=src.id, user_id=USER)


def test_the_same_channel_re_posting_the_entry_is_found(db):
    clint = _source(db, "Clint")
    prior_msg, prior_order = _entry(db, clint)
    hit = discord_repost.find_recent_entry(db, _new_msg(clint), _signal(), now=NOW, window_s=180)
    assert hit is not None and hit[1].id == prior_order.id


def test_another_channel_on_the_same_contract_is_its_own_signal(db):
    clint, mark = _source(db, "Clint"), _source(db, "Mark")
    _entry(db, clint)
    assert discord_repost.find_recent_entry(db, _new_msg(mark), _signal(), now=NOW, window_s=180) is None


@pytest.mark.parametrize("change", [
    dict(ago_s=400),                                    # outside the window
    dict(strike="782"),                                 # another contract
    dict(status=OrderStatus.REJECTED, filled="0"),      # the first never traded: re-posting is how it gets placed
    dict(status=OrderStatus.CANCELED, filled="0"),
])
def test_not_a_re_post_when(db, change):
    clint = _source(db, "Clint")
    _entry(db, clint, **change)
    assert discord_repost.find_recent_entry(db, _new_msg(clint), _signal(), now=NOW, window_s=180) is None


def test_an_average_or_add_is_a_deliberate_second_buy(db):
    clint = _source(db, "Clint")
    _entry(db, clint)
    for extra in (dict(double_up=True), dict(add_to_latest=True)):
        assert discord_repost.find_recent_entry(db, _new_msg(clint), _signal(**extra), now=NOW, window_s=180) is None


def test_an_earlier_average_is_not_what_a_new_entry_repeats(db):
    clint = _source(db, "Clint")
    _entry(db, clint, signal=_signal(price="0.53", double_up=True))
    assert discord_repost.find_recent_entry(db, _new_msg(clint), _signal(), now=NOW, window_s=180) is None


def test_an_unstated_expiry_still_matches(db):
    """"$SPY 781 CALL @0.56" after "$SPY 781 CALL 0DTE @0.63" — the same contract."""
    clint = _source(db, "Clint")
    _entry(db, clint)
    sig = _signal(); sig.pop("expiration")
    assert discord_repost.find_recent_entry(db, _new_msg(clint), sig, now=NOW, window_s=180) is not None


# ── what happens to it ───────────────────────────────────────────────────────

def test_a_filled_first_entry_means_nothing_more_is_bought(db):
    clint = _source(db, "Clint")
    prior_msg, order = _entry(db, clint)
    reason = discord_repost.absorb(db, _new_msg(clint), prior_msg, order, _signal(), now=NOW)
    assert "already filled" in reason and "not bought again" in reason and "30s ago" in reason


def test_a_resting_first_entry_is_moved_to_the_corrected_price(db, monkeypatch):
    clint = _source(db, "Clint")
    prior_msg, order = _entry(db, clint, status=OrderStatus.SUBMITTED, filled="0",
                              otype=OrderType.LIMIT, limit="0.63")
    moved = []
    monkeypatch.setattr(discord_edit, "_attempt", lambda db_, o: moved.append(o.discord_edit_price) or "repriced 0.63 -> 0.56")
    reason = discord_repost.absorb(db, _new_msg(clint), prior_msg, order, _signal(price="0.56"), now=NOW)
    assert moved == [D("0.56")]
    assert "repriced 0.63 -> 0.56" in reason and "not bought again" in reason


def test_a_resting_first_entry_at_the_same_price_is_left_alone(db, monkeypatch):
    clint = _source(db, "Clint")
    prior_msg, order = _entry(db, clint, status=OrderStatus.SUBMITTED, filled="0",
                              otype=OrderType.LIMIT, limit="0.56")
    monkeypatch.setattr(discord_edit, "_attempt", lambda *a: pytest.fail("nothing to move"))
    assert "still working" in discord_repost.absorb(db, _new_msg(clint), prior_msg, order, _signal(price="0.56"), now=NOW)


# ── wired into the alert path (trader and subscribers alike) ─────────────────

def test_a_re_posted_alert_places_no_order(monkeypatch):
    import app.api.discord_sources as ds
    import app.services.discord_execution as ex

    prior = (SimpleNamespace(created_at=NOW), SimpleNamespace(id=uuid.uuid4()))
    monkeypatch.setattr(discord_repost, "find_recent_entry", lambda db, msg, signal: prior)
    monkeypatch.setattr(discord_repost, "absorb", lambda db, msg, pm, o, sig: "Re-posted entry — not bought again.")
    monkeypatch.setattr(ex, "resolve", lambda *a, **k: pytest.fail("must not place a second entry"))
    monkeypatch.setattr(ds.discord_channel_settings, "effective", lambda *a, **k: None)
    msg = SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), user_id=USER, order_id=None,
                          status=DiscordMessageStatus.PARSED, status_reason=None,
                          parsed_signal=_signal(), decision=None)
    monkeypatch.setattr(ex, "already_executed", lambda m: False)
    ds._execute_signal(SimpleNamespace(), SimpleNamespace(id=USER), msg, None, None)
    assert msg.status is DiscordMessageStatus.PARSED and "not bought again" in msg.status_reason


# ── a switched contract ──────────────────────────────────────────────────────

@pytest.fixture
def quiet(monkeypatch):
    """No broker, no copy mirrors, no notification table: record what was asked."""
    seen = SimpleNamespace(fanout=[], notes=[])
    monkeypatch.setattr(discord_repost, "_fanout", lambda oid, bg: seen.fanout.append(oid))
    monkeypatch.setattr(discord_repost, "_notify", lambda db, uid, text, **meta: seen.notes.append(text))
    return seen


def test_the_same_channel_switching_strikes_is_found(db):
    clint = _source(db, "Clint")
    _, first = _entry(db, clint, strike="781")
    hit = discord_repost.find_switched_entry(db, _new_msg(clint), _signal(strike="780"), now=NOW, window_s=180)
    assert hit is not None and hit[1].id == first.id
    # calls -> puts on the same strike is a switch too
    assert discord_repost.find_switched_entry(db, _new_msg(clint), _signal(right="put"), now=NOW, window_s=180) is not None


@pytest.mark.parametrize("why", ["same contract", "other channel", "outside window", "an average", "stock"])
def test_not_a_switch_when(db, why):
    clint, mark = _source(db, "Clint"), _source(db, "Mark")
    _entry(db, clint, ago_s=400 if why == "outside window" else 30)
    src = mark if why == "other channel" else clint
    sig = _signal(strike="781" if why == "same contract" else "780")
    if why == "an average":
        sig["double_up"] = True
    if why == "stock":
        sig = {"action": "BUY", "symbol": "SPY", "asset_type": "STOCK", "limit_price": "670"}
    assert discord_repost.find_switched_entry(db, _new_msg(src), sig, now=NOW, window_s=180) is None


def test_switching_off_an_unfilled_entry_cancels_it(db, quiet):
    clint = _source(db, "Clint")
    prior_msg, first = _entry(db, clint, status=OrderStatus.SUBMITTED, filled="0",
                              otype=OrderType.LIMIT, limit="0.63")
    note = discord_repost.supersede(db, _new_msg(clint), prior_msg, first, _signal(strike="780"), now=NOW)
    assert first.status is OrderStatus.CANCELED
    assert quiet.fanout == [first.id] and quiet.notes == []
    assert "cancelled" in note and "SPY 781C" in note and "SPY 780C" in note
    assert prior_msg.status_reason.startswith("Superseded")


def test_switching_off_a_filled_entry_keeps_it_and_says_so(db, quiet):
    clint = _source(db, "Clint")
    prior_msg, first = _entry(db, clint)                     # filled 2
    note = discord_repost.supersede(db, _new_msg(clint), prior_msg, first, _signal(strike="780"), now=NOW)
    assert first.status is OrderStatus.FILLED and quiet.fanout == []
    assert "still held" in note
    assert len(quiet.notes) == 1 and "You still hold 2 SPY 781C" in quiet.notes[0] and "not sold" in quiet.notes[0]


def test_a_switch_whose_cancel_the_broker_refuses_is_flagged(db, quiet, monkeypatch):
    clint = _source(db, "Clint")
    prior_msg, first = _entry(db, clint, status=OrderStatus.SUBMITTED, filled="0")
    monkeypatch.setattr(discord_repost, "cancel_entry", lambda db_, o: "the broker says it already finished")
    note = discord_repost.supersede(db, _new_msg(clint), prior_msg, first, _signal(strike="780"), now=NOW)
    assert first.status is OrderStatus.SUBMITTED and quiet.fanout == []
    assert "could not be cancelled" in note and len(quiet.notes) == 1


def test_a_switch_still_trades_the_new_contract(monkeypatch):
    import app.api.discord_sources as ds
    import app.services.discord_execution as ex

    switched = (SimpleNamespace(created_at=NOW), SimpleNamespace(id=uuid.uuid4()))
    calls = []
    monkeypatch.setattr(discord_repost, "find_recent_entry", lambda db, msg, signal: None)
    monkeypatch.setattr(discord_repost, "find_switched_entry", lambda db, msg, signal: switched)
    monkeypatch.setattr(discord_repost, "supersede", lambda db, msg, pm, o, sig, background=None: calls.append("supersede") or "switched")
    monkeypatch.setattr(ex, "cancel_stale_entries_for_signal", lambda *a: [])

    class Placed(Exception):
        pass

    def _resolve(*a, **k):
        calls.append("resolve")
        raise Placed

    monkeypatch.setattr(ex, "resolve", _resolve)
    monkeypatch.setattr(ds.discord_channel_settings, "effective", lambda *a, **k: None)
    monkeypatch.setattr(ex, "already_executed", lambda m: False)
    msg = SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), user_id=USER, order_id=None,
                          status=DiscordMessageStatus.PARSED, status_reason=None,
                          parsed_signal=_signal(strike="780"), decision=None)
    ds._execute_signal(SimpleNamespace(), SimpleNamespace(id=USER), msg, None, None)
    assert calls == ["supersede", "resolve"]


# ── an edit that switches the contract ───────────────────────────────────────

def _edited(db, src, *, ago_s=30, decision=None, **entry):
    from app.models.discord_message import SignalDecision

    msg, order = _entry(db, src, ago_s=ago_s, **entry)
    msg.parsed_signal = _signal(strike="780", price="0.50")
    msg.decision = decision or SignalDecision.APPROVED
    db.commit()
    return msg, order


def test_an_edit_to_another_contract_cancels_the_unfilled_entry_and_trades_it(db, quiet):
    clint = _source(db, "Clint")
    msg, first = _edited(db, clint, status=OrderStatus.SUBMITTED, filled="0")
    outcome, trade = discord_repost.switch_on_edit(db, msg, now=NOW)
    assert trade is True and first.status is OrderStatus.CANCELED
    assert msg.order_id is None and msg.status is DiscordMessageStatus.PARSED
    assert "SPY 781C" in outcome and "SPY 780C" in outcome and quiet.fanout == [first.id]


def test_in_manual_mode_the_edited_contract_awaits_approval(db, quiet):
    from app.models.discord_message import SignalDecision

    clint = _source(db, "Clint")
    msg, first = _edited(db, clint, status=OrderStatus.SUBMITTED, filled="0", decision=SignalDecision.PENDING)
    outcome, trade = discord_repost.switch_on_edit(db, msg, now=NOW)
    assert trade is False and first.status is OrderStatus.CANCELED and "awaits your approval" in outcome


def test_an_edit_to_another_contract_after_a_fill_is_flagged_only(db, quiet):
    clint = _source(db, "Clint")
    msg, first = _edited(db, clint)                          # filled 2
    outcome, trade = discord_repost.switch_on_edit(db, msg, now=NOW)
    assert trade is False and msg.order_id == first.id and first.status is OrderStatus.FILLED
    assert "still held" in outcome and len(quiet.notes) == 1


def test_a_late_edit_to_another_contract_is_not_traded(db, quiet, monkeypatch):
    monkeypatch.setattr(discord_repost, "_window_s", lambda: 180)
    clint = _source(db, "Clint")
    msg, first = _edited(db, clint, ago_s=900, status=OrderStatus.SUBMITTED, filled="0")
    outcome, trade = discord_repost.switch_on_edit(db, msg, now=NOW)
    assert trade is False and first.status is OrderStatus.SUBMITTED and "too long after" in outcome


# ── a deleted alert ──────────────────────────────────────────────────────────

def test_deleting_an_alert_cancels_its_unfilled_entry(db, quiet):
    clint = _source(db, "Clint")
    msg, order = _entry(db, clint, status=OrderStatus.SUBMITTED, filled="0")
    outcome = discord_repost.apply_delete(db, msg)
    assert order.status is OrderStatus.CANCELED and quiet.fanout == [order.id]
    assert "cancelled" in outcome and quiet.notes == []


def test_deleting_an_alert_you_are_in_sells_nothing_and_says_so(db, quiet):
    clint = _source(db, "Clint")
    msg, order = _entry(db, clint)                           # filled 2
    outcome = discord_repost.apply_delete(db, msg)
    assert order.status is OrderStatus.FILLED and quiet.fanout == []
    assert "not sold" in outcome and len(quiet.notes) == 1 and "deleted in Discord" in quiet.notes[0]


def test_delete_and_re_post_keeps_the_order(db, quiet, monkeypatch):
    """Clint deletes @0.63 after re-posting @0.56: the re-post was folded into the
    same order (absorb), so the order is the re-post's now."""
    monkeypatch.setattr(discord_repost, "_window_s", lambda: 180)
    clint = _source(db, "Clint")
    msg, order = _entry(db, clint, ago_s=40, status=OrderStatus.SUBMITTED, filled="0")
    db.add(DiscordMessage(id=uuid.uuid4(), source_id=clint.id, user_id=USER, discord_message_id="99",
                          discord_channel_id=clint.channel_id, content="$SPY 781 CALL 0DTE @0.56",
                          status=DiscordMessageStatus.PARSED, parsed_signal=_signal(price="0.56"),
                          status_reason="Re-posted entry — ...", created_at=NOW - timedelta(seconds=10)))
    db.commit()
    outcome = discord_repost.apply_delete(db, msg)
    assert order.status is OrderStatus.SUBMITTED and quiet.fanout == [] and "re-posted" in outcome


def test_deleting_an_exit_alert_leaves_the_exit_alone(db, quiet):
    clint = _source(db, "Clint")
    msg, order = _entry(db, clint, status=OrderStatus.SUBMITTED, filled="0")
    order.side, order.is_closing = OrderSide.SELL, True
    db.commit()
    assert "exit" in discord_repost.apply_delete(db, msg) and order.status is OrderStatus.SUBMITTED


def test_ingest_routes_a_deletion_once(db, monkeypatch):
    from app.services import discord_ingest

    monkeypatch.setattr(discord_ingest, "_emit", lambda *a, **k: None)
    clint = _source(db, "Clint")
    msg, _ = _entry(db, clint)
    gone = {"message_id": msg.discord_message_id, "channel_id": clint.channel_id, "content": "", "is_delete": True}
    report = discord_ingest.ingest_batch(db, clint, [gone], publish=False)
    assert report.deleted == [msg] and not report.accepted
    msg.status_reason = f"{discord_repost.DELETED_PREFIX} — handled"
    db.commit()
    again = discord_ingest.ingest_batch(db, clint, [gone], publish=False)   # a reconnect replays it
    assert again.deleted == [] and again.duplicates == [msg.discord_message_id]
    unknown = discord_ingest.ingest_batch(db, clint, [{**gone, "message_id": "12345"}], publish=False)
    assert unknown.deleted == [] and not unknown.accepted


def test_the_intake_trades_an_edit_that_switched_contracts(monkeypatch):
    import app.api.discord_sources as ds

    placed = []
    monkeypatch.setattr(ds.discord_edit, "apply_price_edit", lambda db, m: discord_edit.DIFFERENT_CONTRACT)
    monkeypatch.setattr(discord_repost, "switch_on_edit", lambda db, m, background=None: ("switched from SPY 781C to SPY 780C", True))
    monkeypatch.setattr(ds, "_execute_signal", lambda db, u, m, bg, rq: placed.append(m))
    msg = SimpleNamespace(discord_message_id="1", status=DiscordMessageStatus.ORDER_CREATED, status_reason=None)
    ds._handle_edits(SimpleNamespace(), SimpleNamespace(id=USER), [msg], None, None)
    assert placed == [msg] and msg.status_reason == "Edited alert: switched from SPY 781C to SPY 780C"


def test_the_intake_marks_a_deleted_alert(monkeypatch):
    import app.api.discord_sources as ds

    monkeypatch.setattr(discord_repost, "apply_delete", lambda db, m, background=None: "the unfilled SPY 781C entry was cancelled")
    msg = SimpleNamespace(discord_message_id="1", status_reason=None)
    ds._handle_deletions(SimpleNamespace(), [msg], None)
    assert msg.status_reason == "Deleted in Discord — the unfilled SPY 781C entry was cancelled"
