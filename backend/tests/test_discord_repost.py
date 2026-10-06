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
