""""Stopped out of rest of SPY calls" closes everything matching from that channel.

Live 2026-10-01 (Clint). The author is fully out; every matching contract the
channel opened and is still held is closed in full at market. Matching ones
opened by hand or from another channel are left alone.
"""
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import discord_sources
from app.models.discord_account import DiscordAccount
from app.models.discord_alert_source import DiscordAlertSource
from app.models.discord_message import DiscordMessage, DiscordMessageStatus
from app.models.discord_position_guard import DiscordPositionGuard
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.user import User, UserRole
from app.services import discord_execution as ex
from app.services.discord_parsers import parse_message
from app.services.discord_parsers.base import ParsedMessage, ParseStatus


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001, ARG001
    return "JSON"


# ── parsing ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,right,strike", [
    ("Stopped out of rest of SPY calls", "CALL", None),
    ("stopped out on $SPY puts", "PUT", None),
    ("Stopped out SPY", None, None),
    ("Stopped out of SPY767C", "CALL", "767"),
    ("Stopped out of rest of SPY calls @here", "CALL", None),
])
def test_a_stop_out_closes_everything_matching(text, right, strike):
    s = parse_message(ParsedMessage(content=text)).signals[0]
    assert (s.action.value, s.symbol, s.close_all_matching, s.flatten) == ("SELL", "SPY", True, True)
    assert (s.option_type.value if s.option_type else None) == right
    assert (str(s.strike) if s.strike is not None else None) == strike


def test_a_stop_out_naming_two_tickers_is_refused():
    assert parse_message(ParsedMessage(content="Stopped out of SPY and QQQ calls")).status is ParseStatus.INVALID


@pytest.mark.parametrize("text, action", [
    ("Stopped out", "STOPPED_OUT"),
    ("Stopped out @here @Sniper", "STOPPED_OUT"),
    ("Cutting @here @Sniper 10% loss", "CUTTING"),        # live 2026-10-07, missed
])
def test_a_close_out_naming_no_ticker_closes_the_channels_position(text, action):
    r = parse_message(ParsedMessage(content=text))
    assert r.status is ParseStatus.PARSED
    s = r.signals[0]
    assert s.action.value == "SELL" and s.symbol is None and s.source_action == action
    assert s.close_all_matching and s.latest_contract and s.flatten


def test_cutting_a_named_ticker_closes_that_ticker():
    s = parse_message(ParsedMessage(content="Cutting IWM @here 10% loss")).signals[0]
    assert s.symbol == "IWM" and s.close_all_matching and not s.latest_contract and s.source_action == "CUTTING"


# ── which contracts: this channel's, still held ─────────────────────────────

USER = uuid.uuid4()
EXP = date(2026, 10, 1)


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (User, DiscordAccount, DiscordAlertSource, DiscordMessage, Order, DiscordPositionGuard):
        m.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    db.add(User(id=USER, email="u@x.com", password_hash="x", role=UserRole.TRADER, is_active=True))
    clint = DiscordAlertSource(user_id=USER, label="Clint", channel_id="1", status="connected")
    other = DiscordAlertSource(user_id=USER, label="Other", channel_id="2", status="connected")
    db.add_all([clint, other]); db.commit()
    return db, clint, other


def _bought(db, source, strike, right, mid):
    o = Order(id=uuid.uuid4(), user_id=USER, instrument_type=InstrumentType.OPTION, symbol="SPY",
              side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=Decimal(2),
              limit_price=Decimal("1"), status=OrderStatus.FILLED, option_expiry=EXP,
              option_strike=Decimal(strike), option_right=right)
    db.add(o); db.flush()
    db.add(DiscordMessage(source_id=source.id, user_id=USER, discord_message_id=mid,
                          discord_channel_id=source.channel_id, content="x",
                          status=DiscordMessageStatus.ORDER_CREATED, order_id=o.id))
    db.commit()


def _pos(strike, right, qty=2):
    return SimpleNamespace(option_strike=Decimal(strike), option_right=right,
                           option_expiry=EXP, quantity=Decimal(qty))


@pytest.fixture
def broker(monkeypatch):
    held = []
    monkeypatch.setattr(ex, "_broker_account", lambda db, user: SimpleNamespace(encrypted_credentials="x"))
    monkeypatch.setattr(ex, "decrypt_json", lambda x: {})
    monkeypatch.setattr(ex, "adapter_for", lambda acct, creds: object())
    monkeypatch.setattr(ex, "_positions", lambda adapter, symbol: list(held))
    return held


def test_only_this_channels_matching_calls_still_held(broker):
    db, clint, other = _db()
    _bought(db, clint, "767", OptionRight.CALL, "1")
    _bought(db, clint, "770", OptionRight.CALL, "2")
    _bought(db, clint, "760", OptionRight.PUT, "3")       # a put: not "calls"
    _bought(db, other, "765", OptionRight.CALL, "4")      # another channel's call
    _bought(db, clint, "775", OptionRight.CALL, "5")      # already closed
    broker.extend([_pos("767", OptionRight.CALL), _pos("770", OptionRight.CALL),
                   _pos("760", OptionRight.PUT), _pos("765", OptionRight.CALL)])
    got = ex.channel_held_contracts(db, db.get(User, USER), clint.id, "SPY", "CALL")
    assert sorted(c["strike"] for c in got) == ["767", "770"]


def _assign(db, source, strike, right):
    """The trader assigned this held contract to ``source`` (Positions → Channel)."""
    db.add(DiscordPositionGuard(user_id=USER, symbol="SPY", option_strike=Decimal(strike),
                                option_right=right.value, option_expiry=EXP, sell_count=0,
                                source_id=source.id))
    db.commit()


def test_a_position_assigned_to_this_channel_counts_as_its_own(broker):
    db, clint, other = _db()
    _bought(db, other, "765", OptionRight.CALL, "1")      # opened by another channel…
    _assign(db, clint, "765", OptionRight.CALL)           # …then assigned to Clint
    broker.append(_pos("765", OptionRight.CALL))
    got = ex.channel_held_contracts(db, db.get(User, USER), clint.id, "SPY", "CALL")
    assert [c["strike"] for c in got] == ["765"]
    assert ex.channel_held_contracts(db, db.get(User, USER), other.id, "SPY", "CALL") == []


def test_a_position_assigned_away_is_no_longer_this_channels(broker):
    db, clint, other = _db()
    _bought(db, clint, "767", OptionRight.CALL, "1")
    _bought(db, clint, "770", OptionRight.CALL, "2")
    _assign(db, other, "770", OptionRight.CALL)
    broker.extend([_pos("767", OptionRight.CALL), _pos("770", OptionRight.CALL)])
    got = ex.channel_held_contracts(db, db.get(User, USER), clint.id, "SPY", "CALL")
    assert [c["strike"] for c in got] == ["767"]


# ── closing them all, one line each ─────────────────────────────────────────

def test_each_contract_is_closed_in_full_and_summarised(monkeypatch):
    contracts = [
        {"symbol": "SPY", "strike": "767", "option_type": "call", "expiration": "2026-10-01"},
        {"symbol": "SPY", "strike": "770", "option_type": "call", "expiration": "2026-10-01"},
    ]
    monkeypatch.setattr(ex, "channel_held_contracts", lambda *a, **k: contracts)
    monkeypatch.setattr(discord_sources, "_channel_exits_manual", lambda *a: False)
    seen = []
    first = uuid.uuid4()

    def _fake_execute(db, user, msg, bg, req):
        seen.append(dict(msg.parsed_signal))
        if msg.parsed_signal["strike"] == "767":
            msg.order_id = first
        else:
            msg.status_reason = "Broker rejected the order: market closed"

    monkeypatch.setattr(discord_sources, "_execute_signal", _fake_execute)
    sig = parse_message(ParsedMessage(content="Stopped out of rest of SPY calls")).signals[0].as_dict()
    msg = SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), parsed_signal=sig,
                          order_id=None, status=None, status_reason=None)
    discord_sources._close_all_from_channel(None, SimpleNamespace(id=USER), msg, None, None)

    assert [s["strike"] for s in seen] == ["767", "770"]
    assert all(s["flatten"] and not s["close_all_matching"] and s["expiration"] for s in seen)
    assert msg.parsed_signal == sig                        # the alert's own reading is kept
    assert msg.order_id == first and msg.status is DiscordMessageStatus.ORDER_CREATED
    assert msg.status_reason == ("Stopped out — SPY 767C 2026-10-01: closed; "
                                 "SPY 770C 2026-10-01: Broker rejected the order: market closed")


def test_nothing_held_from_the_channel_is_refused(monkeypatch):
    monkeypatch.setattr(ex, "channel_held_contracts", lambda *a, **k: [])
    monkeypatch.setattr(discord_sources, "_channel_exits_manual", lambda *a: False)
    sig = parse_message(ParsedMessage(content="Stopped out of rest of SPY calls")).signals[0].as_dict()
    msg = SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), parsed_signal=sig,
                          order_id=None, status=None, status_reason=None)
    discord_sources._close_all_from_channel(None, SimpleNamespace(id=USER), msg, None, None)
    assert msg.status is DiscordMessageStatus.ORDER_FAILED
    assert msg.status_reason == "Stopped out — you hold no SPY calls opened from this channel."


def test_cutting_with_no_ticker_closes_the_contract_the_channel_is_in(monkeypatch):
    latest = {"symbol": "IWM", "strike": "282", "option_type": "put", "expiration": "2026-10-07"}
    monkeypatch.setattr(ex, "latest_channel_contract", lambda db, user, sid: latest)
    asked = {}

    def _held(db, user, sid, symbol, option_type=None, strike=None):
        asked.update(symbol=symbol, option_type=option_type, strike=strike)
        return [latest]

    monkeypatch.setattr(ex, "channel_held_contracts", _held)
    monkeypatch.setattr(discord_sources, "_channel_exits_manual", lambda *a: False)
    seen = []
    monkeypatch.setattr(discord_sources, "_execute_signal",
                        lambda db, user, msg, bg, req: seen.append(dict(msg.parsed_signal)))
    sig = parse_message(ParsedMessage(content="Cutting @here @Sniper 10% loss")).signals[0].as_dict()
    msg = SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), parsed_signal=sig,
                          order_id=None, status=None, status_reason=None)
    discord_sources._close_all_from_channel(None, SimpleNamespace(id=USER), msg, None, None)
    assert asked["symbol"] == "IWM" and asked["option_type"] == "put"
    assert [(s["symbol"], s["strike"], s["flatten"]) for s in seen] == [("IWM", "282", True)]


def test_cutting_with_nothing_held_from_the_channel_is_refused(monkeypatch):
    monkeypatch.setattr(ex, "latest_channel_contract", lambda db, user, sid: None)
    monkeypatch.setattr(discord_sources, "_channel_exits_manual", lambda *a: False)
    sig = parse_message(ParsedMessage(content="Cutting @here 10% loss")).signals[0].as_dict()
    msg = SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), parsed_signal=sig,
                          order_id=None, status=None, status_reason=None)
    discord_sources._close_all_from_channel(None, SimpleNamespace(id=USER), msg, None, None)
    assert msg.status is DiscordMessageStatus.ORDER_FAILED and "nothing from this channel is held" in msg.status_reason


# ── "Adding .4" after being stopped out ──────────────────────────────────────

def test_an_add_with_nothing_held_finds_the_channels_latest_contract_to_re_enter(broker, monkeypatch):
    from datetime import date as _date
    from app.services import market_hours
    monkeypatch.setattr(market_hours, "now_et", lambda: SimpleNamespace(date=lambda: EXP))
    db, clint, other = _db()
    _bought(db, clint, "767", OptionRight.CALL, "1")       # stopped out: nothing held
    user = db.get(User, USER)
    assert ex.latest_channel_contract(db, user, clint.id) is None                 # the old answer
    got = ex.latest_channel_contract(db, user, clint.id, held_only=False)
    assert (got["symbol"], got["strike"], got["option_type"]) == ("SPY", "767.0000", "call")


def test_an_expired_contract_is_not_re_entered(broker, monkeypatch):
    from datetime import timedelta
    from app.services import market_hours
    monkeypatch.setattr(market_hours, "now_et", lambda: SimpleNamespace(date=lambda: EXP + timedelta(days=1)))
    db, clint, other = _db()
    _bought(db, clint, "767", OptionRight.CALL, "1")
    assert ex.latest_channel_contract(db, db.get(User, USER), clint.id, held_only=False) is None
