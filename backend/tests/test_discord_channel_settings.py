"""Alert handling per channel.

A channel follows the account's Discord settings until "Use account settings" is
turned off; then it has its own copy (started from the account's) that it edits
independently. Entries use the alert's channel; exits use the channel that
OPENED the position. A channel can also send its entries at market.
"""
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.discord_sources as ds
import app.api.trades as trades
import app.services.discord_execution as ex
from app.models.discord_account import DiscordAccount
from app.models.discord_alert_source import DiscordAlertSource
from app.models.discord_message import DiscordMessage, DiscordMessageStatus
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.settings import TraderSettings
from app.models.user import User, UserRole
from app.schemas.discord import ChannelSettingsIn, DiscordSettingsIn
from app.schemas.order import PlaceOrderIn
from app.services import discord_channel_settings as dcs


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001, ARG001
    return "JSON"


USER = uuid.uuid4()


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (User, TraderSettings, DiscordAccount, DiscordAlertSource, DiscordMessage, Order):
        m.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    db.add(User(id=USER, email="t@x.com", password_hash="x", role=UserRole.TRADER,
                is_active=True, discord_enabled=True))
    db.add(TraderSettings(user_id=USER, discord_execution_mode="manual", discord_live_trading=False,
                          discord_quantity_multiplier=2, discord_trim_profit_gate_pct=Decimal("20"),
                          discord_auto_trim=False))
    clint = DiscordAlertSource(user_id=USER, label="Clint", channel_id="1", status="connected")
    jpm = DiscordAlertSource(user_id=USER, label="JPM", channel_id="2", status="connected")
    db.add_all([clint, jpm]); db.commit()
    return db, clint, jpm


def _me(db):
    return db.get(User, USER)


# ── following the account, and stopping ─────────────────────────────────────

def test_a_channel_follows_the_account_by_default():
    db, clint, _ = _db()
    eff = dcs.effective(db, USER, clint.id)
    assert eff.discord_quantity_multiplier == 2
    out = ds.get_channel_settings(clint.id, db, _me(db))
    assert out.use_account_settings is True and out.entry_order_type == "limit"
    assert out.quantity_multiplier == 2


def test_turning_it_off_starts_from_the_accounts_values():
    db, clint, _ = _db()
    out = ds.update_channel_settings(clint.id, ChannelSettingsIn(use_account_settings=False), db, _me(db))
    assert out.use_account_settings is False and out.quantity_multiplier == 2
    assert db.get(DiscordAlertSource, clint.id).channel_settings["discord_quantity_multiplier"] == 2


def test_a_channel_edit_changes_only_that_channel():
    db, clint, jpm = _db()
    ds.update_channel_settings(clint.id, ChannelSettingsIn(use_account_settings=False), db, _me(db))
    out = ds.update_channel_settings(
        clint.id, ChannelSettingsIn(quantity_multiplier=5, live_trading=True, auto_trim=True,
                                    trim_profit_gate_pct="35", max_per_order="1000"), db, _me(db))
    assert (out.quantity_multiplier, out.live_trading, out.auto_trim) == (5, True, True)
    assert (out.trim_profit_gate_pct, out.max_per_order) == ("35", "1000")
    acct = db.get(TraderSettings, USER)
    assert acct.discord_quantity_multiplier == 2 and acct.discord_live_trading is False
    assert dcs.effective(db, USER, jpm.id).discord_quantity_multiplier == 2      # still the account
    eff = dcs.effective(db, USER, clint.id)
    assert eff.discord_trim_profit_gate_pct == Decimal("35") and eff.discord_max_per_order == Decimal("1000")


def test_an_account_edit_doesnt_reach_a_channel_with_its_own():
    db, clint, jpm = _db()
    ds.update_channel_settings(clint.id, ChannelSettingsIn(use_account_settings=False), db, _me(db))
    ds.update_discord_settings(DiscordSettingsIn(quantity_multiplier=7), db, _me(db))
    assert dcs.effective(db, USER, clint.id).discord_quantity_multiplier == 2
    assert dcs.effective(db, USER, jpm.id).discord_quantity_multiplier == 7


def test_turning_it_back_on_follows_the_account_again():
    db, clint, _ = _db()
    ds.update_channel_settings(clint.id, ChannelSettingsIn(use_account_settings=False), db, _me(db))
    ds.update_channel_settings(clint.id, ChannelSettingsIn(quantity_multiplier=5), db, _me(db))
    out = ds.update_channel_settings(clint.id, ChannelSettingsIn(use_account_settings=True), db, _me(db))
    assert out.use_account_settings is True and out.quantity_multiplier == 2


def test_editing_a_channel_that_follows_the_account_is_refused():
    db, clint, _ = _db()
    with pytest.raises(HTTPException) as e:
        ds.update_channel_settings(clint.id, ChannelSettingsIn(quantity_multiplier=5), db, _me(db))
    assert e.value.status_code == 409


def test_the_same_validation_applies_to_a_channel():
    db, clint, _ = _db()
    ds.update_channel_settings(clint.id, ChannelSettingsIn(use_account_settings=False), db, _me(db))
    with pytest.raises(HTTPException):
        ds.update_channel_settings(clint.id, ChannelSettingsIn(trim_qty_pct="150"), db, _me(db))


def test_approval_is_per_channel():
    db, clint, jpm = _db()
    ds.update_channel_settings(clint.id, ChannelSettingsIn(use_account_settings=False), db, _me(db))
    ds.update_channel_settings(clint.id, ChannelSettingsIn(execution_mode="auto"), db, _me(db))
    assert ds._auto_approve(db, USER, clint.id) is True
    assert ds._auto_approve(db, USER, jpm.id) is False
    assert ds._auto_approve(db, USER) is False


def test_market_or_limit_is_set_per_channel():
    db, clint, jpm = _db()
    out = ds.update_channel_settings(clint.id, ChannelSettingsIn(entry_order_type="market"), db, _me(db))
    assert out.entry_order_type == "market" and out.use_account_settings is True
    assert dcs.entry_order_type(db, clint.id) == "market"
    assert dcs.entry_order_type(db, jpm.id) == "limit"


# ── an exit uses the channel that opened the position ──────────────────────

def test_the_opening_channels_ladder_is_found_from_the_guard():
    db, clint, _ = _db()
    ds.update_channel_settings(clint.id, ChannelSettingsIn(use_account_settings=False), db, _me(db))
    ds.update_channel_settings(clint.id, ChannelSettingsIn(trim_profit_gate_pct="35"), db, _me(db))
    order = Order(id=uuid.uuid4(), user_id=USER, instrument_type=InstrumentType.OPTION, symbol="SPY",
                  side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=Decimal(1),
                  limit_price=Decimal("1"), status=OrderStatus.FILLED)
    db.add(order); db.flush()
    db.add(DiscordMessage(source_id=clint.id, user_id=USER, discord_message_id="m1",
                          discord_channel_id="1", content="x",
                          status=DiscordMessageStatus.ORDER_CREATED, order_id=order.id))
    db.commit()
    origin = dcs.for_guard(db, USER, SimpleNamespace(entry_order_id=order.id))
    assert origin.discord_trim_profit_gate_pct == Decimal("35")
    assert dcs.for_guard(db, USER, SimpleNamespace(entry_order_id=None)) is None


# ── market entries ──────────────────────────────────────────────────────────

FUTURE = datetime.now(timezone.utc).date() + timedelta(days=7)


@pytest.fixture
def entry(monkeypatch):
    seen = {}
    settings = SimpleNamespace(discord_quantity_multiplier=1, discord_max_per_contract=None,
                               discord_max_per_order=None, discord_live_trading=True)
    monkeypatch.setattr(dcs, "effective", lambda db, uid, sid: settings)
    monkeypatch.setattr(ds.discord_execution, "resolve", lambda *a, **k: ex.Resolved(
        payload=PlaceOrderIn(instrument_type=InstrumentType.OPTION, symbol="SPY", side=OrderSide.BUY,
                             order_type=OrderType.LIMIT, quantity=Decimal(2), limit_price=Decimal("1.38"),
                             option_expiry=FUTURE, option_strike=Decimal("770"),
                             option_right=OptionRight.CALL),
        broker_account_id=uuid.uuid4(), is_closing=False, resolutions={},
        mark_price=None, position_entry_price=None,
    ))
    monkeypatch.setattr(ds.discord_execution, "cancel_stale_entries_for_signal", lambda *a, **k: [])
    monkeypatch.setattr(ds.events, "publish", lambda *a, **k: None)
    monkeypatch.setattr(ds.guards, "on_buy", lambda *a, **k: seen.setdefault("entry_price", k.get("entry_price")))

    def _place(db_, u, payload, acct_id, bg, req, **kw):
        seen["payload"] = payload
        return SimpleNamespace(id=uuid.uuid4(), status=OrderStatus.SUBMITTED)

    monkeypatch.setattr(trades, "_place_trader_order", _place)
    return seen


def _run_entry():
    msg = SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), status=DiscordMessageStatus.PARSED,
                          status_reason=None, order_id=None,
                          parsed_signal={"action": "BUY", "symbol": "SPY"})
    ds._execute_signal(None, SimpleNamespace(id=USER, role=UserRole.TRADER), msg, None, None)
    return msg


def test_a_market_channel_buys_at_market_in_the_session(entry, monkeypatch):
    monkeypatch.setattr(dcs, "entry_order_type", lambda db, sid: "market")
    monkeypatch.setattr("app.services.market_hours.in_regular_session", lambda *a, **k: True)
    _run_entry()
    assert entry["payload"].order_type is OrderType.MARKET and entry["payload"].limit_price is None
    assert entry["entry_price"] == Decimal("1.38")        # the ladder's provisional entry


def test_a_market_channel_keeps_the_limit_outside_the_session(entry, monkeypatch):
    monkeypatch.setattr(dcs, "entry_order_type", lambda db, sid: "market")
    monkeypatch.setattr("app.services.market_hours.in_regular_session", lambda *a, **k: False)
    _run_entry()
    assert entry["payload"].order_type is OrderType.LIMIT and entry["payload"].limit_price == Decimal("1.38")


def test_a_limit_channel_is_unchanged(entry, monkeypatch):
    monkeypatch.setattr(dcs, "entry_order_type", lambda db, sid: "limit")
    monkeypatch.setattr("app.services.market_hours.in_regular_session", lambda *a, **k: True)
    _run_entry()
    assert entry["payload"].order_type is OrderType.LIMIT
