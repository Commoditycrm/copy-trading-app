"""Discord for subscribers: their own copy of each trader channel.

A subscriber following a Discord trader gets one mirror source per trader
channel. Alerts are ingested into it and traded as the SUBSCRIBER, on their own
settings. The mirror's switch is theirs: off skips new entries, exits still go
through. Nothing else about the channel is theirs to change.
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.api import discord_sources
from app.models.discord_account import DiscordAccount
from app.models.discord_alert_source import DiscordAlertSource
from app.models.discord_message import DiscordMessage, DiscordMessageStatus
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.schemas.discord import DiscordSourceUpdateIn
from app.services import discord_ingest, discord_subscribers as ds

TRADER, OTHER_TRADER, SUB = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001, ARG001
    return "JSON"

ENTRY = "AMZN245P @here @Sniper .55"
EXIT = "Holy moly what an add trim @here @Sniper .63 moving crazy AMZN 25%"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(discord_ingest, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(discord_ingest, "_publish", lambda *a, **k: True)


def _db(copy_enabled=True, trader_discord=True):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    for m in (User, SubscriberSettings, TraderSettings, DiscordAccount,
              DiscordAlertSource, DiscordMessage):
        m.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    db.add_all([
        User(id=TRADER, email="t@x.com", password_hash="x", role=UserRole.TRADER,
             is_active=True, discord_enabled=trader_discord),
        User(id=OTHER_TRADER, email="o@x.com", password_hash="x", role=UserRole.TRADER,
             is_active=True, discord_enabled=True),
        User(id=SUB, email="s@x.com", password_hash="x", role=UserRole.SUBSCRIBER, is_active=True),
    ])
    db.flush()
    db.add(SubscriberSettings(user_id=SUB, following_trader_id=TRADER, copy_enabled=copy_enabled))
    db.add(TraderSettings(user_id=TRADER, discord_execution_mode="manual",
                          discord_live_trading=True, discord_quantity_multiplier=4,
                          discord_auto_trim=True, discord_webhook_url="https://hook",
                          discord_alerts_enabled=True))
    db.commit()
    return db


def _channel(db, owner=TRADER, channel_id="111", label="Sniper"):
    src = DiscordAlertSource(user_id=owner, label=label, channel_id=channel_id,
                             channel_name="breakdownsniper", status="connected")
    db.add(src); db.commit()
    return src


def _raw(content, mid="9001"):
    # Posted just now: an alert that arrives late is held for approval
    # (services/discord_freshness), which is not what these tests are about.
    from datetime import datetime, timezone
    return {"message_id": mid, "channel_id": "111", "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat()}


# ── who gets it ──────────────────────────────────────────────────────────────

def test_a_subscriber_of_a_discord_trader_has_discord():
    db = _db()
    assert ds.has_discord(db, db.get(User, SUB))
    assert ds.has_discord(db, db.get(User, TRADER))


def test_a_subscriber_of_a_trader_without_discord_does_not():
    db = _db(trader_discord=False)
    assert not ds.has_discord(db, db.get(User, SUB))


# ── mirrors ──────────────────────────────────────────────────────────────────

def test_every_trader_channel_is_mirrored_but_not_self():
    db = _db()
    a = _channel(db, channel_id="111", label="Sniper")
    _channel(db, channel_id="222", label="JPM")
    _channel(db, channel_id="self", label="Self")
    pairs = ds.sync_mirrors(db, db.get(User, SUB))
    assert sorted(m.label for m, _ in pairs) == ["JPM", "Sniper"]
    mirror = next(m for m, p in pairs if p.id == a.id)
    assert mirror.user_id == SUB and mirror.parent_source_id == a.id
    assert mirror.account_id is None          # never handed to the listener


def test_mirrors_follow_the_trader_they_follow_now():
    db = _db()
    _channel(db)
    ds.sync_mirrors(db, db.get(User, SUB))
    db.get(SubscriberSettings, SUB).following_trader_id = OTHER_TRADER
    _channel(db, owner=OTHER_TRADER, channel_id="333", label="Other")
    pairs = ds.sync_mirrors(db, db.get(User, SUB))
    assert [m.label for m, _ in pairs] == ["Other"]


def test_a_dropped_channel_keeps_the_subscribers_history(executed):
    """The trader removing a channel (or the subscriber following someone else)
    detaches the copy — its messages and order links stay — and the copy is
    re-attached, same row, if the channel comes back."""
    db = _db(); parent = _channel(db)
    _relay(db, ENTRY)
    mirror_id = ds.ensure_mirror(db, db.get(User, SUB), parent).id

    db.get(SubscriberSettings, SUB).following_trader_id = OTHER_TRADER; db.commit()
    ds.sync_mirrors(db, db.get(User, SUB)); db.commit()
    kept = db.get(DiscordAlertSource, mirror_id)
    assert kept is not None and kept.parent_source_id is None
    assert db.execute(select(DiscordMessage).where(DiscordMessage.source_id == mirror_id)).scalars().first()

    db.get(SubscriberSettings, SUB).following_trader_id = TRADER; db.commit()
    pairs = ds.sync_mirrors(db, db.get(User, SUB))
    assert [m.id for m, _ in pairs] == [mirror_id]


def test_settings_start_as_the_traders_on_auto_without_the_webhook():
    db = _db()
    _channel(db)
    ds.sync_mirrors(db, db.get(User, SUB))
    ts = db.get(TraderSettings, SUB)
    assert ts.discord_quantity_multiplier == 4 and ts.discord_auto_trim is True
    assert ts.discord_live_trading is True
    assert ts.discord_execution_mode == "auto"
    assert ts.discord_webhook_url is None and ts.discord_alerts_enabled is False


def test_existing_settings_are_never_overwritten():
    db = _db()
    db.add(TraderSettings(user_id=SUB, discord_quantity_multiplier=2)); db.commit()
    _channel(db)
    ds.sync_mirrors(db, db.get(User, SUB))
    assert db.get(TraderSettings, SUB).discord_quantity_multiplier == 2


# ── relay ────────────────────────────────────────────────────────────────────

@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setattr(discord_sources, "_execute_signal",
                        lambda db, user, msg, bg, req: calls.append((user.id, msg.parsed_signal["action"])))
    return calls


def _relay(db, content, mid="9001"):
    parent = db.execute(select(DiscordAlertSource).where(
        DiscordAlertSource.user_id == TRADER)).scalars().first()
    ds.relay_for_subscriber(db, db.get(User, SUB), parent, [_raw(content, mid)], None, None)
    db.commit()
    return db.execute(select(DiscordMessage).where(DiscordMessage.user_id == SUB)
                      .order_by(DiscordMessage.created_at.desc())).scalars().first()


def test_an_entry_is_traded_as_the_subscriber(executed):
    db = _db(); _channel(db)
    msg = _relay(db, ENTRY)
    assert executed == [(SUB, "BUY")]
    assert msg.user_id == SUB


def test_a_channel_turned_off_skips_entries(executed):
    db = _db(); parent = _channel(db)
    ds.ensure_mirror(db, db.get(User, SUB), parent).is_enabled = False
    msg = _relay(db, ENTRY)
    assert executed == []
    assert msg.status is DiscordMessageStatus.IGNORED
    assert msg.status_reason == ds.CHANNEL_OFF_REASON


def test_a_channel_turned_off_still_takes_exits(executed):
    db = _db(); parent = _channel(db)
    ds.ensure_mirror(db, db.get(User, SUB), parent).is_enabled = False
    _relay(db, EXIT)
    assert executed == [(SUB, "SELL")]


def test_copy_trading_off_skips_entries_but_not_exits(executed):
    db = _db(copy_enabled=False); _channel(db)
    msg = _relay(db, ENTRY, "1")
    assert msg.status_reason == ds.COPY_OFF_REASON
    _relay(db, EXIT, "2")
    assert executed == [(SUB, "SELL")]


def test_the_traders_pause_does_not_reach_the_subscriber(executed):
    """Independent: the trader decides which channels exist, nothing else."""
    db = _db(); _channel(db)
    db.get(TraderSettings, TRADER).copy_paused = True; db.commit()
    _relay(db, ENTRY)
    assert executed == [(SUB, "BUY")]


def test_manual_approval_waits_for_the_subscriber(executed):
    db = _db(); _channel(db)
    ds.ensure_settings(db, db.get(User, SUB), db.get(User, TRADER)).discord_execution_mode = "manual"
    msg = _relay(db, ENTRY)
    assert executed == [] and msg.decision.value == "pending"


def test_self_and_mirror_sources_are_never_relayed(monkeypatch):
    db = _db()
    started = []
    monkeypatch.setattr(ds._pool, "submit", lambda *a: started.append(a))
    selfsrc = _channel(db, channel_id="self", label="Self")
    assert ds.relay_batch(db, selfsrc, [_raw(ENTRY)]) == 0
    assert started == []


# ── the subscriber's switch is the only thing they change ───────────────────

def test_a_subscriber_can_only_switch_a_channel_on_or_off():
    db = _db(); parent = _channel(db)
    mirror = ds.ensure_mirror(db, db.get(User, SUB), parent); db.commit()
    sub = db.get(User, SUB)
    out = discord_sources.update_source(mirror.id, DiscordSourceUpdateIn(is_enabled=False), db, sub)
    assert out.is_enabled is False and out.mirrored is True
    with pytest.raises(HTTPException) as e:
        discord_sources.update_source(mirror.id, DiscordSourceUpdateIn(label="Mine"), db, sub)
    assert e.value.status_code == 403


def test_a_late_entry_is_held_for_the_subscriber_too():
    """The backlog a reconnecting listener replays reaches subscribers as well;
    their late entries wait for approval just like the trader's."""
    import inspect
    from app.services import discord_subscribers as subs
    src = inspect.getsource(subs.relay_for_subscriber)
    assert "discord_freshness.hold_if_stale(msg)" in src
    assert src.index("hold_if_stale") < src.index("_execute_signal")
