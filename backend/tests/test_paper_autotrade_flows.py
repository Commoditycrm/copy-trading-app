"""QA paper auto-trading through the real trading paths, with simulated
placement (no broker, no Alpaca): Discord auto-execution on a governed paper
account, which copies are allowed, and that the paper identity is required
for every order, exits and stop moves included."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import app.api.discord_sources as ds
import app.services.copy_engine as ce
from app.brokers.alpaca import AlpacaAdapter
from app.models.broker_account import BrokerName
from app.models.user import UserRole
from app.services import paper_autotrade as pa
from test_discord_manual_exit import _settings, exit_alert  # noqa: F401 — fixture
from test_self_mirror_loop import _acct, _by_sub, _follow, _trader_buy, _user, conn, db, fanout_env  # noqa: F401

PAPER = {"api_key": "PKTEST", "api_secret": "s", "paper": True}
LIVE = {"api_key": "AKLIVE", "api_secret": "s", "paper": False}


# ── Discord auto-execution ────────────────────────────────────────────────


def test_a_simulate_only_channel_on_a_governed_paper_account_is_sent(exit_alert, monkeypatch):
    setup, placed = exit_alert
    db_, user, msg, _, _ = setup(_settings(discord_live_trading=False))
    seen = []
    monkeypatch.setattr(pa, "governs", lambda aid: seen.append(aid) or True)
    ds._execute_signal(db_, user, msg, background=None, request=None)
    assert "payload" in placed and len(seen) == 1                    # sent to the (simulated) broker


def test_the_same_channel_on_any_other_account_stays_simulate_only(exit_alert, monkeypatch):
    setup, placed = exit_alert
    db_, user, msg, _, _ = setup(_settings(discord_live_trading=False))
    monkeypatch.setattr(pa, "governs", lambda aid: False)
    ds._execute_signal(db_, user, msg, background=None, request=None)
    assert placed == {} and "Paper mode" in (msg.status_reason or "")


def test_a_live_channel_is_unchanged_and_not_asked(exit_alert, monkeypatch):
    setup, placed = exit_alert
    db_, user, msg, _, _ = setup(_settings(discord_live_trading=True))
    monkeypatch.setattr(pa, "governs", lambda aid: pytest.fail("not consulted for a live channel"))
    ds._execute_signal(db_, user, msg, background=None, request=None)
    assert "payload" in placed


# ── copies of a governed account ──────────────────────────────────────────


@pytest.fixture
def copies(db, fanout_env, monkeypatch):
    creds_by_acct: dict = {}
    monkeypatch.setattr(ce.cache, "decrypt_creds_cached", lambda aid, _enc: creds_by_acct.get(aid, {}))
    monkeypatch.setattr(ce, "adapter_for", __import__("app.brokers", fromlist=["adapter_for"]).adapter_for)
    monkeypatch.setattr(ce.audit, "record", lambda *a, **k: None)
    sent = []

    def _place(item):
        sent.append(item)
        return SimpleNamespace(status=ce.OrderStatus.SUBMITTED, broker_order_id="SIM-" + item.child_order_id.hex[:8],
                               submitted_at=datetime.now(timezone.utc), filled_quantity=0,
                               filled_avg_price=None, filled_at=None, bracket_legs=[])
    monkeypatch.setattr(ce, "_place_mirror_with_conflict_resolve", _place)
    return SimpleNamespace(creds=creds_by_acct, sent=sent)


def _world(db, fanout_env, copies):
    trader = _user(db, UserRole.TRADER)
    subs = {k: _user(db, UserRole.SUBSCRIBER) for k in ("paper", "live", "webull", "sim")}
    db.flush()
    t_acct = _acct(db, trader.id, "PA1TRADER", broker=BrokerName.ALPACA)
    accts = {
        "paper": _acct(db, subs["paper"].id, "PA2PAPER", broker=BrokerName.ALPACA),
        "live": _acct(db, subs["live"].id, "999888777", broker=BrokerName.ALPACA),
        "webull": _acct(db, subs["webull"].id, "WB1", broker=BrokerName.WEBULL),
        "sim": _acct(db, subs["sim"].id, "SIM1", broker=BrokerName.FAKE),
    }
    copies.creds.update({accts["paper"].id: PAPER, accts["live"].id: LIVE, accts["webull"].id: {"paper": False}})
    for k, u in subs.items():
        _follow(db, u.id, trader.id)
        fanout_env.accts_by_user[u.id] = [accts[k]]
    order = _trader_buy(db, trader, t_acct)
    db.flush()
    return trader, t_acct, subs, accts, order


def test_copies_of_a_governed_account_go_only_to_paper_or_simulated(db, fanout_env, copies, monkeypatch):
    trader, t_acct, subs, accts, order = _world(db, fanout_env, copies)
    monkeypatch.setattr(pa, "governs", lambda aid: aid == t_acct.id)
    st = _by_sub(asyncio.run(ce.fanout_async(db, order, trader)))
    assert st[subs["paper"].id] == ["submitted"] and st[subs["sim"].id] == ["submitted"]
    assert st[subs["live"].id] == ["skipped_not_paper"] and st[subs["webull"].id] == ["skipped_not_paper"]
    by_acct = {i.broker_account_id: i for i in copies.sent}
    assert set(by_acct) == {accts["paper"].id, accts["sim"].id}
    assert isinstance(by_acct[accts["paper"].id].adapter, AlpacaAdapter)
    assert by_acct[accts["paper"].id].adapter.paper_guard is not None   # proven paper again at placement


def test_without_governance_copies_are_exactly_as_before(db, fanout_env, copies, monkeypatch):
    trader, t_acct, subs, accts, order = _world(db, fanout_env, copies)
    monkeypatch.setattr(pa, "governs", lambda aid: False)
    monkeypatch.setattr(ce, "adapter_for", lambda acct, creds: SimpleNamespace(paper_guard=None))
    st = _by_sub(asyncio.run(ce.fanout_async(db, order, trader)))
    assert st[subs["live"].id] == ["submitted"] and st[subs["paper"].id] == ["submitted"]
    assert all(getattr(i.adapter, "paper_guard", None) is None for i in copies.sent)


# ── the paper identity is always required ─────────────────────────────


def test_an_unproven_account_refuses_exits_too(monkeypatch):
    monkeypatch.setattr(pa, "verify_identity", lambda *a, **k: "client is not on the paper host")
    adapter = pa.attach(AlpacaAdapter(LIVE), uuid.uuid4(), LIVE)
    adapter._client = object()                                             # noqa: SLF001
    for req, kind in ((SimpleNamespace(is_closing=True), "place"), (SimpleNamespace(is_closing=False), "replace")):
        with pytest.raises(pa.PaperGuardError, match="paper host"):
            adapter.paper_guard(req, kind)
