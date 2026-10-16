"""The "Self" channel: replaying an alert the system missed.

A virtual DiscordAlertSource so that everything downstream — parser, sizing
caps, trim ladder, guards, the Channel column — treats a hand-submitted alert
exactly like one that arrived from Discord. The value of the feature is that
there is NO second path into the order pipeline; a bespoke one would be the
thing nobody tests.
"""
import inspect
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.api.discord_sources as ds

# The endpoint is a thin wrapper now; the pipeline lives in the core that
# both it and auto-trim call.
_SRC = inspect.getsource(ds.submit_self_alert_text)


# ── it reuses the real pipeline, it does not reimplement one ────────────────

def test_it_goes_through_the_normal_ingest():
    """The same function the listener's own POST calls. A hand-rolled parse
    here would drift from the live one the first time either changed."""
    assert "discord_ingest.ingest_batch(" in _SRC


def test_it_executes_through_the_normal_path():
    assert "_execute_signal(db, user, msg, background or _InlineTasks(), request)" in _SRC


def test_it_never_accepts_a_pre_parsed_signal():
    """Only the TEXT is taken. Letting a caller hand in a parsed signal would
    be a second, untested way to reach the broker."""
    fields = set(ds.DiscordSelfAlertIn.model_fields)
    # The text, and which of the trader's channels to handle it AS — never a
    # parsed signal, a symbol, a quantity or a price.
    assert fields == {"content", "source_id"}


def test_it_honours_the_traders_execution_mode():
    """"As if Discord had delivered it" includes the auto/manual gate — in
    manual it must land awaiting approval, not place."""
    # …the chosen channel's own mode, when the alert is typed as a channel.
    assert "_auto_approve(db, user.id, src.id if typed_into_channel else None)" in _SRC
    assert "if auto and msg.decision is SignalDecision.APPROVED:" in _SRC
    # The composer never forces approval — only auto-trim does, and only
    # because turning auto-trim on IS the approval.
    endpoint = inspect.getsource(ds.submit_self_alert)
    assert "approve=True" not in endpoint


# ── the source itself ───────────────────────────────────────────────────────

class _DB:
    def __init__(self, existing=None):
        self.existing = existing
        self.added = []

    def execute(self, stmt):
        row = self.existing
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: row))

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


def _user():
    return SimpleNamespace(id=uuid.uuid4(), email="t@example.com")


def test_the_self_source_is_created_on_demand():
    db = _DB()
    src = ds._self_source(db, _user())
    assert db.added == [src]
    assert src.channel_id == ds._SELF_CHANNEL_ID
    assert src.label == ds._SELF_LABEL


def test_an_existing_self_source_is_reused():
    """One per trader. A second would split the alert history in two and give
    the Channel column two different answers for the same thing."""
    existing = SimpleNamespace(channel_id="self", label="Self")
    db = _DB(existing=existing)
    assert ds._self_source(db, _user()) is existing
    assert db.added == []


def test_the_self_source_has_no_discord_account():
    """This is what makes it virtual. listener_assignments INNER JOINs sources
    to DiscordAccount, so a source with no account is never handed to a
    watcher — nothing had to be excluded by name."""
    src = ds._self_source(_DB(), _user())
    assert src.account_id is None
    assert "DiscordAccount.id == DiscordAlertSource.account_id" in inspect.getsource(
        ds.listener_assignments
    )


def test_the_reserved_channel_id_cannot_collide_with_a_real_one():
    """Discord channel ids are numeric snowflakes."""
    assert not ds._SELF_CHANNEL_ID.isdigit()


def test_it_is_always_available():
    """A schedule would mean "you may not replay a missed alert right now",
    which is the opposite of the point."""
    src = ds._self_source(_DB(), _user())
    assert src.schedule_mode == "always"
    assert src.is_enabled is True


# ── the synthetic message id ────────────────────────────────────────────────

def test_the_message_id_is_numeric_and_increasing():
    """Ingest orders a source's messages by this, compared as an int."""
    ids = [ds._self_message_id() for _ in range(3)]
    assert all(i.isdigit() for i in ids)
    assert [int(i) for i in ids] == sorted(int(i) for i in ids)


def test_the_message_id_fits_the_column():
    assert len(ds._self_message_id()) <= 40


# ── the Channel column ──────────────────────────────────────────────────────

def test_order_history_shows_it_as_self():
    """_fill_channels prefers the source's label, which is "Self"."""
    from app.api.trades import _fill_channels

    src = inspect.getsource(_fill_channels)
    assert "(label or \"\").strip() or (channel_name or \"\").strip()" in src
    assert ds._self_source(_DB(), _user()).label == "Self"


# ── it must not appear as a channel the trader manages ──────────────────────

def test_the_self_source_is_hidden_from_the_channel_list():
    """It is plumbing, not a channel anyone connected. Listing it would offer
    Change channel / Disconnect / a watch schedule for something with no
    Discord behind it — and disconnecting it would break the composer with no
    way to get it back."""
    src = inspect.getsource(ds.list_sources)
    assert "DiscordAlertSource.channel_id != _SELF_CHANNEL_ID" in src


def test_hiding_it_does_not_hide_its_orders():
    """The Channel column resolves the name by joining messages to sources
    directly, so an order placed through Self still reads "Self" in Order
    History even though the channel list never mentions it."""
    import uuid
    from types import SimpleNamespace

    from app.api.trades import _attach_discord_channel

    order = SimpleNamespace(id=uuid.uuid4(), discord_channel=None)
    rows = [(order.id, "Self", "Self", "self")]
    db = SimpleNamespace(execute=lambda stmt: SimpleNamespace(all=lambda: rows))
    _attach_discord_channel(db, [order])
    assert order.discord_channel == "Self"


# ── typed AS a channel (the composer's channel picker) ───────────────────────

def test_an_alert_can_be_typed_as_one_of_the_traders_channels():
    """It is stored on THAT channel, so its settings size and manage the trade
    and the position shows it — but only a channel the trader owns."""
    assert "_get_owned(db, user, source_id) if source_id is not None else _self_source(db, user)" in _SRC


def test_a_typed_alert_is_not_the_channels_heartbeat():
    """ingest_batch treats an arriving message as proof the watcher is live. A
    typed one proves nothing about the watcher, and the disconnect watchdog
    reads that heartbeat — so the channel's watcher markers are put back."""
    assert "src.last_heartbeat_at, src.last_message_at, src.last_seen_message_id = watcher_state" in _SRC


def test_typed_alerts_are_recognised_whatever_channel_they_were_typed_as():
    """Manual exits let through what the trader types. The composer signs each
    alert with the trader's own id as author — a real Discord author never is."""
    import uuid
    uid = uuid.uuid4()
    typed = SimpleNamespace(author_id=str(uid), user_id=uid, source_id=uuid.uuid4())
    assert ds._is_self_alert(None, typed) is True
    from_discord = SimpleNamespace(author_id="112233445566778899", user_id=uid, source_id=None)
    assert ds._is_self_alert(None, from_discord) is False


def test_at_market_is_taken_off_before_parsing_and_marked_on_the_signal(monkeypatch):
    """"... @Market" is an instruction, not part of the alert: the parser sees
    the alert alone, and execution sees at_market on its signal."""
    seen = {}
    src = SimpleNamespace(id=uuid.uuid4(), channel_id="self", last_heartbeat_at=None,
                          last_message_at=None, last_seen_message_id=None)
    stored = SimpleNamespace(parsed_signal={"action": "BUY", "symbol": "SPY"}, parsed_signals=None,
                             decision=None)

    def _ingest(db, source, batch, auto_approve):
        seen["content"] = batch[0]["content"]
        return SimpleNamespace(stored=[stored])

    monkeypatch.setattr(ds, "_self_source", lambda db, user: src)
    monkeypatch.setattr(ds, "_auto_approve", lambda *a, **k: False)
    monkeypatch.setattr(ds.discord_ingest, "ingest_batch", _ingest)
    db = SimpleNamespace(commit=lambda: None, refresh=lambda m: None)
    ds.submit_self_alert_text(db, SimpleNamespace(id=uuid.uuid4(), email="t@x.com"), "SPY 770C 1.38 @Market")
    assert seen["content"] == "SPY 770C 1.38"
    assert stored.parsed_signal["at_market"] is True and stored.parsed_signals[0]["at_market"] is True
