"""Discord connection alerts, and the Entry / Exit pills on each channel.

Watchdog: when a trader's channels stop being read — an error / sign-in
needed, or the listener going quiet (no heartbeat for 5 min; QA 2026-10-01 the
listener stopped and every channel sat "disconnected") — the trader is told
ONCE (in-app + SMS under "Broker connection"), and again only after a recovery.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.discord_sources as ds
from app.models.discord_account import DiscordAccount
from app.models.discord_alert_source import DiscordAlertSource
from app.models.discord_message import DiscordMessage
from app.models.settings import TraderSettings
from app.models.user import User, UserRole
from app.schemas.discord import ChannelSettingsIn, DiscordSourceOut
from app.services import discord_watchdog as wd
from app.services import notifications


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001, ARG001
    return "JSON"


T = uuid.uuid4()
NOW = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (User, TraderSettings, DiscordAccount, DiscordAlertSource, DiscordMessage):
        m.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    db.add(User(id=T, email="t@x.com", password_hash="x", role=UserRole.TRADER,
                is_active=True, discord_enabled=True))
    db.add(TraderSettings(user_id=T, discord_quantity_multiplier=4, discord_live_trading=True,
                          discord_execution_mode="auto", discord_auto_trim=False,
                          discord_trim_profit_gate_pct=Decimal("20"),
                          discord_trim2_profit_gate_pct=Decimal("35"),
                          discord_trim3_profit_gate_pct=Decimal("0")))
    db.commit()
    return db


def _src(db, label, *, hb_ago=30, status="connected", enabled=True):
    s = DiscordAlertSource(user_id=T, label=label, channel_id=label, status=status, is_enabled=enabled,
                           last_heartbeat_at=(NOW - timedelta(seconds=hb_ago)) if hb_ago is not None else None)
    db.add(s); db.commit()
    return s


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notifications, "create_notification", lambda db, **kw: out.append(kw))
    monkeypatch.setattr(wd, "_redis", lambda: (_ for _ in ()).throw(RuntimeError("no redis")))
    monkeypatch.setattr("app.services.discord_schedule.in_window", lambda **kw: True)
    wd._local_alerted.clear()
    return out


def test_connected_channels_send_nothing(sent):
    db = _db(); _src(db, "Clint"); _src(db, "Julia")
    assert wd.check_once(db, NOW) == [] and sent == []


def test_a_quiet_listener_is_reported_once_naming_the_channels(sent):
    db = _db(); _src(db, "Clint", hb_ago=600, status="disconnected"); _src(db, "Julia", hb_ago=600)
    assert wd.check_once(db, NOW) == [T]
    assert sent[0]["type"] == "discord.disconnected"
    assert "Clint" in sent[0]["message"] and "Julia" in sent[0]["message"]
    assert wd.check_once(db, NOW + timedelta(minutes=1)) == []          # not again


def test_a_recovery_resets_it(sent):
    db = _db(); s = _src(db, "Clint", hb_ago=600)
    wd.check_once(db, NOW)
    s.last_heartbeat_at = NOW; db.commit()
    assert wd.check_once(db, NOW) == []                                   # recovered
    s.last_heartbeat_at = NOW - timedelta(minutes=10); db.commit()
    assert wd.check_once(db, NOW) == [T]                                  # a new outage
    assert len(sent) == 2


def test_a_channel_needing_a_sign_in_is_reported(sent):
    db = _db(); _src(db, "Clint", hb_ago=10, status="needs_login")
    assert wd.check_once(db, NOW) == [T]


def test_a_short_blip_is_not_reported(sent):
    db = _db(); _src(db, "Clint", hb_ago=120)                             # 2 min: a restart
    assert wd.check_once(db, NOW) == []


@pytest.mark.parametrize("kwargs", [
    {"enabled": False, "hb_ago": 600},                 # switched off by the trader
    {"hb_ago": None, "status": "needs_login"},         # never connected yet
])
def test_channels_that_arent_meant_to_be_open_are_skipped(sent, kwargs):
    db = _db(); _src(db, "Clint", **kwargs)
    assert wd.check_once(db, NOW) == []


def test_outside_the_watch_window_is_skipped(sent, monkeypatch):
    monkeypatch.setattr("app.services.discord_schedule.in_window", lambda **kw: False)
    db = _db(); _src(db, "Clint", hb_ago=600)
    assert wd.check_once(db, NOW) == []


def test_it_texts_under_the_broker_connection_setting():
    assert notifications._sms_pref_attr("discord.disconnected") == "sms_on_broker_connection"


# ── Entry / Exit pills ──────────────────────────────────────────────────────

def _pills(db, src):
    out = DiscordSourceOut.model_construct()
    ds._with_pills(db, db.get(User, T), src.id, out)
    return out.entry_summary, out.exit_summary


def test_pills_follow_the_account():
    db = _db(); s = _src(db, "Clint")
    assert _pills(db, s) == ("Limit · 4 contracts", "On trim alerts")


def test_pills_show_a_channels_own_settings():
    db = _db(); s = _src(db, "Clint")
    me = db.get(User, T)
    ds.update_channel_settings(s.id, ChannelSettingsIn(use_account_settings=False, entry_order_type="market"), db, me)
    ds.update_channel_settings(s.id, ChannelSettingsIn(quantity_multiplier=1, live_trading=False,
                                                       execution_mode="manual", auto_trim=True), db, me)
    assert _pills(db, s) == ("Market · 1 contract · Test · Review", "Auto-trim 20% / 35%")


def test_the_ai_engine_shows_as_ai_trimming():
    db = _db(); s = _src(db, "Clint")
    db.get(TraderSettings, T).discord_exit_engine = "ai"; db.commit()
    assert _pills(db, s)[1] == "AI trimming"
