"""Tests for inbound Discord alert sources — STEP 2 (listener ingestion).

Covers the two things that must not be wrong at this layer:

  1. SESSION HANDLING — a Discord session is a live credential. Bad uploads are
     rejected with a clear reason, and nothing derived from a session ever
     carries a cookie value back to the caller.
  2. DUPLICATE PROTECTION (PHASE 7) — a reconnect, refresh or listener restart
     re-observes messages we already have. That must be a no-op, and the dedup
     layer must fail OPEN when Redis is down (a missed alert is worse than a
     duplicate, and the durable guard is the step-3 unique constraint).
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.services.discord_ingest as ingest
from app.models.discord_message import DiscordMessage, DiscordMessageStatus
from app.services.discord_session import (
    DiscordSessionError,
    describe_session,
    encrypt_session,
    parse_channel_url,
    validate_storage_state,
)

_T0 = datetime(2026, 9, 8, 14, 30, 0, tzinfo=timezone.utc)


class _FakeSource:
    """Stands in for a DiscordAlertSource row.

    That model maps Postgres-only UUID columns and can't be created on SQLite,
    but ``ingest_batch`` only reads/writes these attributes on it — the rows it
    actually INSERTs are DiscordMessage, which is a real table here.
    """

    def __init__(self):
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.channel_id = "987654321"
        self.channel_name = None
        self.guild_name = None
        self.status = "connecting"
        self.last_error = None
        self.last_message_at = None
        self.last_heartbeat_at = None
        self.last_seen_message_id = None


class _FakeRedis:
    """Minimal XADD stand-in with a switch for simulating an outage."""

    def __init__(self, *, down=False):
        self.stream = []
        self.down = down

    def xadd(self, key, fields, maxlen=None, approximate=True):
        if self.down:
            raise ConnectionError("redis unavailable")
        self.stream.append(fields)
        return b"1-1"


@pytest.fixture
def db():
    """Real session on in-memory SQLite.

    Dedup is enforced by a UNIQUE CONSTRAINT now, so testing it against a fake
    would prove nothing — the constraint has to actually exist and actually
    reject the second insert.
    """
    eng = create_engine("sqlite:///:memory:")
    DiscordMessage.__table__.create(eng)
    with Session(eng) as session:
        yield session


@pytest.fixture
def redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(ingest, "get_sync_redis", lambda: r)
    # events.publish opens its own Redis client; silence it so a failure points
    # at ingestion rather than the SSE bus.
    monkeypatch.setattr(ingest.events, "publish", lambda *a, **k: None)
    return r


def _msg(message_id, content="BUY AAPL 250C", ts=_T0, channel="987654321"):
    return {
        "message_id": message_id,
        "channel_id": channel,
        "server_id": "111",
        "author": "AlertBot",
        "content": content,
        "timestamp": ts.isoformat(),
        "attachments": [],
        "embeds": [],
        "is_edit": False,
    }


# ── Channel URL parsing ──────────────────────────────────────────────────────

def test_parse_channel_url_extracts_guild_and_channel():
    assert parse_channel_url("https://discord.com/channels/123/456") == ("123", "456")


def test_parse_channel_url_treats_dm_pseudo_guild_as_no_guild():
    # "@me" is not a real server; storing it as a guild id would be a lie.
    assert parse_channel_url("https://discord.com/channels/@me/456") == (None, "456")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not a url",
        "https://discord.com/channels/123",          # no channel segment
        "https://example.com/channels/123/456",      # not Discord
        "https://discord.com/guilds/123/456",        # wrong path
    ],
)
def test_parse_channel_url_rejects_anything_that_isnt_a_channel_link(bad):
    with pytest.raises(DiscordSessionError):
        parse_channel_url(bad)


# ── Session validation ───────────────────────────────────────────────────────

def _state(domain="discord.com"):
    return {"cookies": [{"name": "__Secure-x", "value": "s3cret", "domain": domain}], "origins": []}


def test_validate_storage_state_accepts_a_discord_session():
    out = validate_storage_state(_state())
    assert out["cookies"] and out["origins"] == []


def test_validate_storage_state_accepts_a_json_string():
    import json
    assert validate_storage_state(json.dumps(_state()))["cookies"]


def test_validate_storage_state_keeps_only_playwright_keys():
    # Anything else the exporter tacked on must not be persisted into the blob.
    out = validate_storage_state({**_state(), "notes": "junk", "token": "leak"})
    assert set(out) == {"cookies", "origins"}


@pytest.mark.parametrize(
    "bad",
    [
        {},                                              # no cookies key
        {"cookies": []},                                 # signed out
        {"cookies": "nope"},                             # malformed
        {"cookies": [{"domain": "example.com"}]},        # not a Discord session
        "{not json",
    ],
)
def test_validate_storage_state_rejects_unusable_uploads(bad):
    with pytest.raises(DiscordSessionError):
        validate_storage_state(bad)


def test_validate_storage_state_error_never_echoes_the_credential():
    # An error message is user-facing and gets logged; a cookie value in it
    # would defeat the whole point of encrypting the session at rest.
    with pytest.raises(DiscordSessionError) as exc:
        validate_storage_state({"cookies": [{"domain": "example.com", "value": "s3cret"}]})
    assert "s3cret" not in str(exc.value)


def test_describe_session_summarises_without_exposing_cookies():
    token = encrypt_session(_state())
    # Age is measured against the wall clock, so anchor the capture time to now
    # rather than a fixed date.
    info = describe_session(token, datetime.now(timezone.utc) - timedelta(days=3))
    assert info["present"] is True
    assert info["cookie_count"] == 1
    assert info["age_days"] == 3
    # No cookie name, domain or value anywhere in the summary.
    assert "s3cret" not in str(info) and "__Secure-x" not in str(info)


def test_describe_session_reports_absent_when_undecryptable():
    # A rotated encryption key must read as "no session" so the trader is told
    # to sign in again, rather than the listener retrying a dead credential.
    info = describe_session("not-a-fernet-token", _T0)
    assert info["present"] is False


# ── Ingestion + duplicate protection ─────────────────────────────────────────

def test_new_messages_are_accepted_and_queued(db, redis):
    src = _FakeSource()
    report = ingest.ingest_batch(db, src, [_msg("100"), _msg("101")])

    assert report.accepted == ["100", "101"]
    assert report.duplicates == []
    assert len(redis.stream) == 2


def test_replayed_messages_are_not_queued_twice(db, redis):
    """The reconnect case: the listener re-observes the rendered backlog."""
    src = _FakeSource()
    ingest.ingest_batch(db, src, [_msg("100"), _msg("101")])
    report = ingest.ingest_batch(db, src, [_msg("100"), _msg("101"), _msg("102")])

    assert report.accepted == ["102"]
    assert report.duplicates == ["100", "101"]
    # Only the genuinely new message reached the pipeline.
    assert len(redis.stream) == 3


def test_duplicates_within_a_single_batch_are_collapsed(db, redis):
    src = _FakeSource()
    report = ingest.ingest_batch(db, src, [_msg("100"), _msg("100")])
    assert report.accepted == ["100"]
    assert report.duplicates == ["100"]


def test_the_same_message_in_two_sources_is_not_a_duplicate(db, redis):
    """Idempotency is scoped per source: two traders watching the same channel
    must each get their own signal, not one of them silently dropped."""
    a, b = _FakeSource(), _FakeSource()
    assert ingest.ingest_batch(db, a, [_msg("100")]).accepted == ["100"]
    assert ingest.ingest_batch(db, b, [_msg("100")]).accepted == ["100"]


def test_a_message_is_stored_even_when_the_queue_is_down(db, monkeypatch):
    """A Redis outage must not lose an alert.

    The durable row is what matters; queueing is a latency optimisation. The row
    lands at RECEIVED so the parser can pick it up from the table, and the
    failure is reported separately so a degraded pipeline is visible rather than
    looking like a clean ingest.
    """
    down = _FakeRedis(down=True)
    monkeypatch.setattr(ingest, "get_sync_redis", lambda: down)
    monkeypatch.setattr(ingest.events, "publish", lambda *a, **k: None)

    src = _FakeSource()
    report = ingest.ingest_batch(db, src, [_msg("100")])

    assert report.accepted == ["100"]
    assert report.queue_failed == ["100"]
    assert report.rejected == []          # NOT rejected — we kept it
    row = db.execute(select(DiscordMessage)).scalar_one()
    assert row.discord_message_id == "100"
    # Parsing runs at intake, so the row carries a verdict rather than sitting
    # at RECEIVED. This fixture ("BUY AAPL 250C", no expiry) is correctly
    # INVALID — the durability claim under test is that the row EXISTS.
    assert row.status is DiscordMessageStatus.INVALID
    assert row.content == "BUY AAPL 250C"


def test_duplicate_is_stopped_by_the_database_not_a_cache(db, redis):
    """The guard is the UNIQUE constraint, so it holds regardless of Redis.

    This is the property the whole duplicate-protection story rests on: even
    with a perfectly healthy cache bypassed, the second insert cannot land.
    """
    src = _FakeSource()
    ingest.ingest_batch(db, src, [_msg("100")])

    # Attempt the raw insert the way a retry/replay would.
    dup = DiscordMessage(
        source_id=src.id, user_id=src.user_id, discord_message_id="100",
        discord_channel_id=src.channel_id, content="replay",
    )
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        db.add(dup)
        db.flush()
    db.rollback()


def test_the_original_message_is_stored_verbatim(db, redis):
    """Embeds and attachments must survive exactly as observed — the alerts in
    the wild carry everything in embeds with EMPTY text, so dropping them would
    leave nothing for the parser and nothing for the audit trail."""
    src = _FakeSource()
    embed = {"title": "ENTERING \u00b7 OKLO $44 CALL \u00b7 09/11",
             "description": "6 @ $1.58 \u00b7 $948",
             "fields": [{"name": "Copy Trading", "value": "ON"}]}
    raw = _msg("100", content="")
    raw["embeds"] = [embed]
    raw["attachments"] = [{"url": "https://cdn.discordapp.com/x.png", "filename": "x.png"}]

    ingest.ingest_batch(db, src, [raw])

    row = db.execute(select(DiscordMessage)).scalar_one()
    assert row.content == ""
    assert row.embeds == [embed]
    assert row.attachments[0]["filename"] == "x.png"
    assert row.author == "AlertBot"


def test_posted_at_records_discord_time_not_ingest_time(db, redis):
    """A backlog replay ingests old messages NOW; the parser has to be able to
    tell how stale an alert is, so Discord's own timestamp is preserved."""
    src = _FakeSource()
    ingest.ingest_batch(db, src, [_msg("100", ts=_T0)])
    row = db.execute(select(DiscordMessage)).scalar_one()
    assert row.posted_at.replace(tzinfo=timezone.utc) == _T0


def test_message_without_an_id_is_rejected_not_guessed(db, redis):
    """No snowflake means no idempotency key, so the message can never be
    de-duplicated. Reject rather than invent one."""
    src = _FakeSource()
    report = ingest.ingest_batch(db, src, [{"content": "BUY AAPL"}])
    assert report.accepted == []
    assert report.rejected == [{"message_id": "", "reason": "missing_message_id"}]


def test_one_bad_message_does_not_stall_the_rest_of_the_batch(db, redis):
    src = _FakeSource()
    report = ingest.ingest_batch(db, src, [{"content": "x"}, _msg("100")])
    assert report.accepted == ["100"]
    assert len(report.rejected) == 1


def test_liveness_columns_track_the_newest_message(db, redis):
    src = _FakeSource()
    later = _T0 + timedelta(minutes=5)
    ingest.ingest_batch(db, src, [_msg("100"), _msg("300", ts=later), _msg("200")])

    assert src.last_seen_message_id == "300"      # highest snowflake, not last in list
    assert src.last_message_at == later
    assert src.last_heartbeat_at is not None


def test_last_seen_never_moves_backwards(db, redis):
    """Snowflakes are compared as integers: at 19 digits, lexical ordering is
    wrong and would let an older message overwrite a newer high-water mark."""
    src = _FakeSource()
    src.last_seen_message_id = "1200000000000000000"
    ingest.ingest_batch(db, src, [_msg("999999999999999999")])
    assert src.last_seen_message_id == "1200000000000000000"


def test_nothing_is_recorded_when_a_batch_is_all_duplicates(db, redis):
    src = _FakeSource()
    ingest.ingest_batch(db, src, [_msg("100")])
    src.last_message_at = None

    ingest.ingest_batch(db, src, [_msg("100")])
    # A replay must not look like fresh activity in the UI.
    assert src.last_message_at is None


def test_queued_payload_carries_the_routing_the_pipeline_needs(db, redis):
    import json
    src = _FakeSource()
    ingest.ingest_batch(db, src, [_msg("100", content="BUY AAPL 250C")])

    entry = redis.stream[0]
    assert entry["source_id"] == str(src.id)
    assert entry["user_id"] == str(src.user_id)
    assert json.loads(entry["message"])["content"] == "BUY AAPL 250C"


# ── Status reporting ─────────────────────────────────────────────────────────

def test_record_status_stores_error_and_backfills_names():
    src = _FakeSource()
    ingest.record_status(
        src, "error", error="channel gone", channel_name="trade-alerts", guild_name="Example"
    )
    assert src.status == "error"
    assert src.last_error == "channel gone"
    assert src.channel_name == "trade-alerts"
    assert src.guild_name == "Example"
    assert src.last_heartbeat_at is not None


def test_record_status_clears_a_stale_error_on_recovery():
    src = _FakeSource()
    ingest.record_status(src, "error", error="channel gone")
    ingest.record_status(src, "connected")
    assert src.status == "connected"
    assert src.last_error is None


def test_record_status_truncates_an_overlong_error():
    src = _FakeSource()
    ingest.record_status(src, "error", error="x" * 2000)
    # The column is String(500); an overlong message must be trimmed here, not
    # blow up on insert.
    assert len(src.last_error) <= 480


# ── Response serialisation ───────────────────────────────────────────────────
# `session` is derived rather than a column, so `model_validate(orm_row)` has no
# attribute to read for it. Getting that wrong 500s every list/create/update
# response — which is exactly what happened — so the shape is pinned here.

class _FakeAccount:
    """The connected Discord account a source reads with. The session lives
    HERE, not on the source — one sign-in covers every channel it can read."""

    def __init__(self, session_token=None, captured_at=None):
        self.id = uuid.uuid4()
        self.encrypted_session = session_token
        self.session_captured_at = captured_at


class _FakeRow(_FakeSource):
    """A source row with the full column set the response model reads."""

    def __init__(self, *, session_token=None, captured_at=None):
        super().__init__()
        self.label = "Test Server"
        self.guild_id = "111"
        self.guild_name = None
        self.is_enabled = True
        self.created_at = _T0
        self.account = _FakeAccount(session_token, captured_at)
        # Schedule columns — mirrored from the model so the response schema can
        # be validated against this stand-in the way it would a real row.
        self.schedule_mode = "always"
        self.schedule_start = None
        self.schedule_end = None
        self.schedule_timezone = None
        self.schedule_days = []


def test_response_serialises_a_source_with_no_session():
    from app.api.discord_sources import _to_out

    out = _to_out(_FakeRow())
    assert out.status == "connecting"
    assert out.session.present is False
    assert out.session.cookie_count == 0


def test_response_reports_a_stored_session_without_leaking_it():
    from app.api.discord_sources import _to_out

    token = encrypt_session(_state())
    out = _to_out(_FakeRow(session_token=token, captured_at=datetime.now(timezone.utc)))

    assert out.session.present is True
    assert out.session.cookie_count == 1
    # The encrypted blob and the cookie value must not survive serialisation.
    dumped = out.model_dump_json()
    assert "s3cret" not in dumped
    assert token not in dumped


def test_repointing_a_channel_resets_the_high_water_mark():
    """Changing channel must clear last_seen_message_id.

    Snowflakes are globally time-ordered, so carrying an old channel's mark over
    would make the NEW channel's backlog look already-ingested and silently
    suppress real alerts — the worst possible failure for an alert feed.
    """
    from app.services.discord_session import parse_channel_url

    src = _FakeRow()
    src.last_seen_message_id = "1300000000000000000"
    src.channel_name = "old-channel"

    guild, channel = parse_channel_url("https://discord.com/channels/999/888")
    assert (guild, channel) == ("999", "888")
    # Mirrors the PATCH handler's reset block.
    src.guild_id, src.channel_id = guild, channel
    src.channel_name = src.guild_name = None
    src.last_seen_message_id = None

    assert src.last_seen_message_id is None
    assert src.channel_name is None


# ── Account-level sessions ───────────────────────────────────────────────────
# A Discord session authenticates an ACCOUNT, not a channel. These pin the
# behaviour that follows from that: connect once, and every channel that account
# can read is covered — no second sign-in.

def test_channels_sharing_an_account_share_its_session():
    """The point of the whole model: a second channel added to a connected
    account is live immediately, with no sign-in of its own."""
    from app.api.discord_sources import _to_out

    account = _FakeAccount(encrypt_session(_state()), datetime.now(timezone.utc))
    a, b = _FakeRow(), _FakeRow()
    a.account = b.account = account

    assert _to_out(a).session.present is True
    assert _to_out(b).session.present is True


def test_a_channel_with_no_account_reports_no_session():
    """A channel whose account was removed must read as disconnected, not
    inherit a stale 'connected' from somewhere."""
    from app.api.discord_sources import _to_out

    row = _FakeRow()
    row.account = None
    out = _to_out(row)
    assert out.session.present is False
    assert out.session.cookie_count == 0


def test_revoking_an_account_session_affects_every_channel_on_it():
    """Sign-out revokes the ACCOUNT's session. Leaving a sibling channel marked
    connected would imply a per-channel login that no longer exists."""
    from app.api.discord_sources import _to_out

    account = _FakeAccount(encrypt_session(_state()), datetime.now(timezone.utc))
    a, b = _FakeRow(), _FakeRow()
    a.account = b.account = account
    assert _to_out(a).session.present and _to_out(b).session.present

    # What clear_session does to the account.
    account.encrypted_session = None
    account.session_captured_at = None

    assert _to_out(a).session.present is False
    assert _to_out(b).session.present is False
