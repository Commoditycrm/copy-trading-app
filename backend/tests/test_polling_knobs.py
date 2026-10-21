"""Every polling interval the admin API page exposes is a runtime knob: read on
the owning loop's hot path, clamped to bounds that encode a real broker limit,
and falling back to the env default whenever Redis can't answer.

The point of these tests is the WIRING. A knob the page writes and no loop
reads is worse than no knob, so each consumer is asserted to actually consult
platform_config rather than a module constant.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.platform_config as pc
from app.models.broker_account import BrokerName


class _FakeRedis:
    """Minimal GET/SET/DELETE. ``boom=True`` makes every read raise, standing in
    for an unreachable cache tier."""

    def __init__(self, data=None, boom=False):
        self.data = dict(data or {})
        self.boom = boom

    def get(self, key):
        if self.boom:
            raise RuntimeError("redis down")
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(pc, "get_sync_redis", lambda: r)
    return r


# ── registry integrity ──────────────────────────────────────────────────

def test_every_knob_names_a_real_setting():
    # A typo in `setting` would make the knob silently read nothing, so the
    # env default must resolve for every one of them.
    from app.config import get_settings
    s = get_settings()
    for knob in pc.INT_KNOBS:
        # Some are float Settings (webull_poll_interval_seconds = 15.0);
        # _env_int casts, so any number is fine — the point is that the
        # attribute resolves at all.
        assert isinstance(getattr(s, knob.setting), (int, float)), knob.name
    for knob in pc.BOOL_KNOBS:
        assert isinstance(getattr(s, knob.setting), bool), knob.name


def test_knob_names_are_unique_and_bounds_sane():
    names = [k.name for k in pc.INT_KNOBS] + [k.name for k in pc.BOOL_KNOBS]
    assert len(names) == len(set(names))
    for knob in pc.INT_KNOBS:
        assert 0 < knob.lo < knob.hi
        # The shipped default has to be inside its own bounds, or the page
        # would render an override-less knob as out of range.
        from app.config import get_settings
        assert knob.lo <= getattr(get_settings(), knob.setting) <= knob.hi


def test_alpaca_knob_shares_the_legacy_redis_key():
    # The pre-existing /config/alpaca-pnl-poll-interval route writes
    # _K_ALPACA_PNL_POLL_INTERVAL. The new generic route must read the SAME key
    # or the two admin surfaces would disagree.
    knob = next(k for k in pc.INT_KNOBS if k.name == "alpaca_pnl_poll_interval_s")
    assert knob.key == pc._K_ALPACA_PNL_POLL_INTERVAL


# ── int knobs ───────────────────────────────────────────────────────────

def test_int_knob_uses_env_default_when_no_override(fake_redis):
    from app.config import get_settings
    assert pc.get_int_knob("webull_pnl_poll_interval_s") == \
        get_settings().webull_pnl_poll_interval_s


def test_int_knob_reads_override(fake_redis):
    pc.set_int_knob("webull_pnl_poll_interval_s", 45)
    assert pc.get_int_knob("webull_pnl_poll_interval_s") == 45


def test_int_knob_none_clears_override(fake_redis):
    from app.config import get_settings
    pc.set_int_knob("webull_pnl_poll_interval_s", 45)
    pc.set_int_knob("webull_pnl_poll_interval_s", None)
    assert pc.get_int_knob("webull_pnl_poll_interval_s") == \
        get_settings().webull_pnl_poll_interval_s


@pytest.mark.parametrize("name,bad", [
    ("webull_pnl_poll_interval_s", 1),       # below the 5s floor
    ("webull_pnl_poll_interval_s", 3000),    # above the 300s ceiling
    ("snaptrade_pnl_poll_interval_s", 5),    # shared quota ⇒ 30s floor
    ("snaptrade_poll_interval_s", 1),        # may be relaxed, never tightened
])
def test_setter_rejects_out_of_bounds(fake_redis, name, bad):
    with pytest.raises(ValueError):
        pc.set_int_knob(name, bad)


def test_getter_clamps_a_rogue_stored_value(fake_redis):
    # Something wrote straight to Redis, or bounds were tightened after the
    # fact. The poll loop must not run at 1s because of it.
    from app.config import get_settings
    knob = pc._INT_BY_NAME["snaptrade_pnl_poll_interval_s"]
    fake_redis.data[knob.key] = "1"
    assert pc.get_int_knob("snaptrade_pnl_poll_interval_s") == \
        get_settings().snaptrade_pnl_poll_interval_s


def test_getter_survives_garbage_and_dead_redis(monkeypatch):
    from app.config import get_settings
    default = get_settings().webull_pnl_poll_interval_s

    knob = pc._INT_BY_NAME["webull_pnl_poll_interval_s"]
    monkeypatch.setattr(pc, "get_sync_redis", lambda: _FakeRedis({knob.key: "soon"}))
    assert pc.get_int_knob("webull_pnl_poll_interval_s") == default

    monkeypatch.setattr(pc, "get_sync_redis", lambda: _FakeRedis(boom=True))
    assert pc.get_int_knob("webull_pnl_poll_interval_s") == default


# ── bool knobs ──────────────────────────────────────────────────────────

def test_bool_knob_default_override_and_clear(fake_redis):
    from app.config import get_settings
    name = "snaptrade_webhook_enabled"
    assert pc.get_bool_knob(name) is get_settings().snaptrade_webhook_enabled
    pc.set_bool_knob(name, True)
    assert pc.get_bool_knob(name) is True
    pc.set_bool_knob(name, False)
    assert pc.get_bool_knob(name) is False
    pc.set_bool_knob(name, None)
    assert pc.get_bool_knob(name) is get_settings().snaptrade_webhook_enabled


def test_bool_knob_ignores_unparseable_value(monkeypatch):
    from app.config import get_settings
    knob = pc._BOOL_BY_NAME["webull_direct_poll_enabled"]
    monkeypatch.setattr(pc, "get_sync_redis", lambda: _FakeRedis({knob.key: "maybe"}))
    assert pc.get_bool_knob("webull_direct_poll_enabled") is \
        get_settings().webull_direct_poll_enabled


# ── state payload the page renders from ─────────────────────────────────

def test_state_reports_override_unset_truthfully(fake_redis):
    state = pc.get_polling_knobs_state()
    by_name = {k["name"]: k for k in state["intervals"]}
    row = by_name["ibkr_pnl_poll_interval_s"]
    assert row["override"] is None
    assert row["effective"] == row["default"]
    assert row["min"] < row["max"]
    assert row["why"]           # the page explains every knob's floor
    assert {k["name"] for k in state["switches"]} == {
        k.name for k in pc.BOOL_KNOBS
    }


def test_state_hides_an_out_of_bounds_override(fake_redis):
    # Stored value the getter clamps away must not be shown as the override,
    # or the page would claim a cadence the poller isn't using.
    knob = pc._INT_BY_NAME["snaptrade_poll_interval_s"]
    fake_redis.data[knob.key] = "1"
    row = next(
        k for k in pc.get_polling_knobs_state()["intervals"]
        if k["name"] == "snaptrade_poll_interval_s"
    )
    assert row["override"] is None
    assert row["effective"] == row["default"]


# ── consumers actually read the knobs ───────────────────────────────────

def test_pnl_poller_reads_the_knob_for_every_mapped_broker(monkeypatch):
    from app.services import pnl_poller

    seen = []

    def _fake_get_int_knob(name):
        seen.append(name)
        return 42

    monkeypatch.setattr(pc, "get_int_knob", _fake_get_int_knob)
    for broker in (BrokerName.ALPACA, BrokerName.WEBULL,
                   BrokerName.SNAPTRADE, BrokerName.IBKR):
        assert pnl_poller._base_interval_for_broker(broker) == 42.0
    assert set(seen) == set(pnl_poller._KNOB_BY_BROKER.values())


def test_pnl_poller_knob_names_all_exist():
    from app.services import pnl_poller
    for name in pnl_poller._KNOB_BY_BROKER.values():
        assert name in pc._INT_BY_NAME, name


def test_pnl_poller_falls_back_when_knob_lookup_explodes(monkeypatch):
    from app.services import pnl_poller

    def _boom(_name):
        raise RuntimeError("redis down")

    monkeypatch.setattr(pc, "get_int_knob", _boom)
    assert pnl_poller._base_interval_for_broker(BrokerName.WEBULL) == \
        pnl_poller._INTERVAL_BY_BROKER[BrokerName.WEBULL]


def test_snaptrade_listener_poll_interval_follows_knobs(monkeypatch):
    from app.services import snaptrade_listener as sl

    monkeypatch.setattr(pc, "get_bool_knob", lambda _n: False)
    monkeypatch.setattr(pc, "get_int_knob", lambda _n: 25)
    assert sl._poll_interval() == 25.0

    # Webhook on ⇒ the backstop wins and the interval knob is ignored.
    monkeypatch.setattr(pc, "get_bool_knob", lambda _n: True)
    assert sl._poll_interval() == sl.POLL_INTERVAL_BACKSTOP_S


def test_snaptrade_listener_falls_back_to_env_when_redis_dies(monkeypatch):
    from app.config import get_settings
    from app.services import snaptrade_listener as sl

    def _boom(_n):
        raise RuntimeError("redis down")

    monkeypatch.setattr(pc, "get_bool_knob", _boom)
    monkeypatch.setattr(pc, "get_int_knob", _boom)
    s = get_settings()
    expected = (
        sl.POLL_INTERVAL_BACKSTOP_S if s.snaptrade_webhook_enabled
        else float(s.snaptrade_poll_interval_s)
    )
    assert sl._poll_interval() == expected


def test_webull_poll_switch_is_a_runtime_knob(monkeypatch):
    from app.services import webull_listener as wl

    monkeypatch.setattr(pc, "get_bool_knob", lambda _n: False)
    assert wl._poll_enabled() is False
    monkeypatch.setattr(pc, "get_bool_knob", lambda _n: True)
    assert wl._poll_enabled() is True


def test_webull_poll_switch_falls_back_to_env(monkeypatch):
    from app.config import get_settings
    from app.services import webull_listener as wl

    def _boom(_n):
        raise RuntimeError("redis down")

    monkeypatch.setattr(pc, "get_bool_knob", _boom)
    assert wl._poll_enabled() is get_settings().webull_direct_poll_enabled


def test_webull_poller_is_spawned_even_when_disabled():
    # Gating the SPAWN on the switch meant re-enabling it at runtime did
    # nothing — there was no task left to notice. The switch must be checked
    # inside the loop instead.
    import inspect
    from app.services import webull_listener as wl

    src = inspect.getsource(wl.start_listener)
    assert "if _poll_enabled():" not in src, \
        "poller spawn must not be gated on the switch"
    assert "_run_poller" in src

    loop_src = inspect.getsource(wl._run_poller)
    assert "if not _poll_enabled():" in loop_src, \
        "the poll loop must re-check the switch every cycle"
    assert "_POLL_DISABLED_IDLE_S" in loop_src


# ── admin endpoints ─────────────────────────────────────────────────────
# Called directly rather than over HTTP, matching the rest of this suite.




def test_get_polling_config_returns_every_knob(fake_redis):
    from app.api.admin import get_polling_config

    out = get_polling_config(_=None)
    assert {k["name"] for k in out["intervals"]} == {k.name for k in pc.INT_KNOBS}
    assert {k["name"] for k in out["switches"]} == {k.name for k in pc.BOOL_KNOBS}
    assert out["groups"] == list(pc.GROUP_ORDER)
    # No request-rate estimate: it was arithmetic dressed as telemetry.
    assert "budget" not in out


def test_patch_sets_and_clears_an_interval(fake_redis):
    from app.api.admin import set_polling_config, PollingConfigIn

    out = set_polling_config(
        payload=PollingConfigIn(name="webull_pnl_poll_interval_s", value=45),
        _=None,
    )
    row = next(k for k in out["intervals"] if k["name"] == "webull_pnl_poll_interval_s")
    assert row["override"] == 45 and row["effective"] == 45

    out = set_polling_config(
        payload=PollingConfigIn(name="webull_pnl_poll_interval_s", value=None),
        _=None,
    )
    row = next(k for k in out["intervals"] if k["name"] == "webull_pnl_poll_interval_s")
    assert row["override"] is None and row["effective"] == row["default"]


def test_patch_rejects_out_of_bounds_with_422(fake_redis):
    from fastapi import HTTPException

    from app.api.admin import set_polling_config, PollingConfigIn

    with pytest.raises(HTTPException) as exc:
        set_polling_config(
            payload=PollingConfigIn(name="snaptrade_pnl_poll_interval_s", value=5),
            _=None,
        )
    assert exc.value.status_code == 422


def test_patch_rejects_an_unknown_knob(fake_redis):
    from fastapi import HTTPException

    from app.api.admin import set_polling_config, PollingConfigIn

    with pytest.raises(HTTPException) as exc:
        set_polling_config(
            payload=PollingConfigIn(name="not_a_knob", value=10),
            _=None,
        )
    assert exc.value.status_code == 404


def test_patch_toggles_a_switch(fake_redis):
    from app.api.admin import set_polling_config, PollingConfigIn

    out = set_polling_config(
        payload=PollingConfigIn(name="webull_direct_poll_enabled", value=False),
        _=None,
    )
    row = next(
        k for k in out["switches"] if k["name"] == "webull_direct_poll_enabled"
    )
    assert row["override"] is False and row["effective"] is False


# ── groups ──────────────────────────────────────────────────────────────

def test_every_knob_belongs_to_a_rendered_group():
    # A knob whose group isn't in GROUP_ORDER would be written by the API and
    # never shown, which is the same failure as a knob nothing reads.
    for knob in list(pc.INT_KNOBS) + list(pc.BOOL_KNOBS):
        assert knob.group in pc.GROUP_ORDER, knob.name


def test_state_exposes_the_group_render_order(fake_redis):
    state = pc.get_polling_knobs_state()
    assert state["groups"] == list(pc.GROUP_ORDER)
    for row in state["intervals"] + state["switches"]:
        assert row["group"] in state["groups"]


# ── no knob name is ever misspelled at a call site ──────────────────────

def test_no_call_site_references_an_unknown_knob():
    """Scan app/ for every knob name passed to a knob accessor and assert it
    resolves. A typo here is silent: the page writes a key nothing reads, and
    the loop quietly keeps its old cadence forever."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    pattern = re.compile(
        r'(?:int_knob_or|get_int_knob|bool_knob_or|get_bool_knob|set_int_knob'
        r'|set_bool_knob)\(\s*"([a-z0-9_]+)"'
    )
    known = set(pc._INT_BY_NAME) | set(pc._BOOL_BY_NAME)
    bad: list[str] = []
    found = 0
    for path in root.rglob("*.py"):
        for name in pattern.findall(path.read_text()):
            found += 1
            if name not in known:
                bad.append(f"{path.relative_to(root)}: {name}")
    assert not bad, "unknown knob names at call sites: " + "; ".join(bad)
    # Guard the guard: if the regex ever stops matching, this test would pass
    # vacuously and stop protecting anything.
    assert found >= 15, f"only matched {found} call sites — regex likely broken"


# ── each consumer reads its own knob ────────────────────────────────────

def test_webull_order_poll_interval_follows_the_knob(monkeypatch):
    from app.services import webull_listener as wl

    monkeypatch.setattr(pc, "int_knob_or", lambda _n, _f: 40.0)
    assert wl._poll_interval() == 40.0

    # The rate-limit floor is applied AFTER the knob, so no admin value can
    # push the cadence under what Webull allows.
    monkeypatch.setattr(pc, "int_knob_or", lambda _n, _f: 0.1)
    assert wl._poll_interval() == wl._DAYORDERS_MIN_INTERVAL_S


def test_webull_closed_interval_knobs_are_read(monkeypatch):
    from app.services import market_hours, webull_listener as wl

    monkeypatch.setattr(market_hours, "market_session", lambda: market_hours.CLOSED)
    monkeypatch.setattr(wl, "_has_working_orders", lambda _t: False)

    seen = []

    def _knob(name, fallback):
        seen.append(name)
        return 300.0

    monkeypatch.setattr(pc, "int_knob_or", _knob)
    import uuid as _uuid
    interval, session, active = wl._poll_plan(1, _uuid.uuid4())
    assert session == market_hours.CLOSED and active is False
    assert interval == 300.0
    assert "webull_poll_interval_closed_seconds" in seen

    # With working orders it reads the OTHER knob.
    seen.clear()
    monkeypatch.setattr(wl, "_has_working_orders", lambda _t: True)
    wl._poll_plan(1, _uuid.uuid4())
    assert "webull_poll_interval_closed_active_seconds" in seen


def test_webull_subscriber_cadences_follow_knobs(monkeypatch):
    from app.services import webull_subscriber_reconciler as wsr

    monkeypatch.setattr(pc, "int_knob_or",
                        lambda n, _f: 7.0 if "idle" not in n else 77.0)
    fast, idle, window = wsr._intervals()
    assert fast == 7.0 and idle == 77.0
    # The fast WINDOW decides which accounts are hot, not how often we call,
    # so it stays env-only rather than becoming a rate knob.
    from app.config import get_settings
    assert window == float(get_settings().webull_subscriber_sync_fast_window_s)


def test_ibkr_subscriber_cadences_follow_knobs(monkeypatch):
    from app.services import ibkr_subscriber_reconciler as isr

    monkeypatch.setattr(pc, "int_knob_or",
                        lambda n, _f: 9.0 if "idle" not in n else 99.0)
    assert isr._fast_interval() == 9.0
    assert isr._idle_interval() == 99.0


def test_webull_positions_cache_ttl_follows_the_knob(monkeypatch):
    from app.brokers import webull as wb

    monkeypatch.setattr(pc, "int_knob_or", lambda _n, _f: 12.0)
    assert wb._positions_ttl() == 12.0


def test_balance_staleness_follows_the_knob(monkeypatch):
    from app.api import brokers as br

    monkeypatch.setattr(pc, "int_knob_or", lambda _n, _f: 600.0)
    assert br._balance_stale_s() == 600.0


def test_day_pnl_share_window_follows_the_knob(monkeypatch):
    from app.api import positions as po

    monkeypatch.setattr(pc, "int_knob_or", lambda _n, _f: 30.0)
    assert po._day_pnl_share_s() == 30


def test_nudge_tunables_follow_knobs(monkeypatch):
    from app.services import snaptrade_nudge as sn

    monkeypatch.setattr(pc, "int_knob_or", lambda _n, _f: 3.0)
    assert sn._read_delay() == 3.0
    assert sn._retry_delay() == 3.0
    assert sn._max_reads() == 3
    assert sn._budget_per_min() == 3.0


def test_nudge_bucket_refills_at_the_knob_rate(monkeypatch):
    from app.services import snaptrade_nudge as sn

    monkeypatch.setattr(pc, "int_knob_or", lambda _n, _f: 60.0)
    bucket = sn._Bucket()
    # Starting fill comes from the knob, not a value frozen at import time.
    assert bucket.tokens == 60.0


@pytest.mark.parametrize("module,knob", [
    ("ibkr_listener", "ibkr_order_poll_interval_s"),
    ("alpaca_subscriber_reconciler", "alpaca_subscriber_reconcile_interval_s"),
    ("snaptrade_listener", "snaptrade_subscriber_reconcile_interval_s"),
    ("balance_sync", "balance_sync_interval_s"),
    ("pnl_snapshot", "pnl_snapshot_interval_s"),
    ("close_reconciler", "close_reconcile_interval_s"),
])
def test_loop_sleeps_on_its_knob(module, knob):
    """These read the knob inline in their sleep, so there is no getter to call.
    Assert the loop names the right knob — the typo scan above covers whether
    the name resolves, this covers whether the loop uses the intended one."""
    import importlib
    import inspect

    mod = importlib.import_module(f"app.services.{module}")
    src = inspect.getsource(mod)
    assert f'int_knob_or(\n            "{knob}"' in src or f'"{knob}"' in src, \
        f"{module} does not reference {knob}"
    assert "int_knob_or" in src, f"{module} never calls a knob accessor"


# ── budget covers the newly tunable order feeds ─────────────────────────




def test_response_model_does_not_strip_any_payload_field(fake_redis):
    """FastAPI's response_model drops fields it doesn't declare, silently. This
    caught a real bug: `group` and `groups` reached the handler and were
    stripped before the page saw them, so every card rendered into no section.
    """
    from app.api.admin import (
        PollingConfigOut,
        PollingKnobOut,
        PollingSwitchOut,
        get_polling_config,
    )

    payload = get_polling_config(_=None)
    assert set(payload.keys()) <= set(PollingConfigOut.model_fields), \
        "top-level payload field not declared on the response model"
    assert set(payload["intervals"][0]) <= set(PollingKnobOut.model_fields)
    assert set(payload["switches"][0]) <= set(PollingSwitchOut.model_fields)

    # And the serialised form still carries what the page keys off.
    serialised = PollingConfigOut(**payload).model_dump()
    assert serialised["groups"]
    assert serialised["intervals"][0]["group"]
    assert serialised["switches"][0]["group"]


def test_non_duration_knobs_declare_their_kind():
    """A count or a rate rendered in a seconds column with a sec/min/hr picker
    is wrong, so the two that aren't durations must say so."""
    by_name = {k.name: k for k in pc.INT_KNOBS}
    assert by_name["snaptrade_nudge_max_reads"].kind == "count"
    assert by_name["snaptrade_nudge_budget_per_min"].kind == "rate"
    # Everything else is seconds, and every kind is one the page handles.
    for knob in pc.INT_KNOBS:
        assert knob.kind in {"seconds", "count", "rate"}, knob.name
        assert knob.unit_label
        if knob.name not in {
            "snaptrade_nudge_max_reads", "snaptrade_nudge_budget_per_min",
        }:
            assert knob.kind == "seconds", knob.name


def test_state_carries_kind_through(fake_redis):
    rows = {k["name"]: k for k in pc.get_polling_knobs_state()["intervals"]}
    assert rows["snaptrade_nudge_max_reads"]["kind"] == "count"
    assert rows["snaptrade_nudge_max_reads"]["unit_label"] == "reads"
    assert rows["webull_pnl_poll_interval_s"]["kind"] == "seconds"


# ── the per-broker matrix ───────────────────────────────────────────────

def test_every_knob_has_matrix_coordinates():
    for knob in list(pc.INT_KNOBS) + list(pc.BOOL_KNOBS):
        assert knob.row, knob.name
        assert knob.broker is None or knob.broker in pc.BROKER_ORDER, knob.name


def test_no_two_knobs_share_a_matrix_cell():
    # Two knobs in one (broker, row) cell would make the grid render one and
    # silently hide the other.
    seen: set[tuple[str, str]] = set()
    for knob in list(pc.INT_KNOBS) + list(pc.BOOL_KNOBS):
        if knob.broker is None:
            continue
        cell = (knob.broker, knob.row)
        assert cell not in seen, f"duplicate cell {cell}"
        seen.add(cell)


def test_matrix_rows_are_grouped_and_cover_every_per_broker_knob():
    rows = pc._matrix_rows()
    # Grouped in GROUP_ORDER: a group's rows are contiguous and in order.
    order = [r["group"] for r in rows]
    assert order == sorted(order, key=pc.GROUP_ORDER.index)
    # Every per-broker knob's row is present exactly once.
    labels = [r["row"] for r in rows]
    assert len(labels) == len(set(labels))
    for knob in list(pc.INT_KNOBS) + list(pc.BOOL_KNOBS):
        if knob.broker is not None:
            assert knob.row in labels, knob.name


def test_switch_rows_sort_above_the_intervals_they_gate():
    rows = [r["row"] for r in pc._matrix_rows()
            if r["group"] == pc.GROUP_ORDERS]
    switch_rows = {k.row for k in pc.BOOL_KNOBS if k.group == pc.GROUP_ORDERS}
    first_interval = min(
        i for i, r in enumerate(rows) if r not in switch_rows
    )
    assert all(rows.index(sr) < first_interval for sr in switch_rows)


def test_na_notes_point_at_genuinely_empty_cells(fake_redis):
    # A note on a cell that HAS a knob would never be shown, and would mean the
    # note and the registry disagree about what exists.
    filled = {
        (k.broker, k.row)
        for k in list(pc.INT_KNOBS) + list(pc.BOOL_KNOBS)
        if k.broker is not None
    }
    row_labels = {r["row"] for r in pc._matrix_rows()}
    for (broker, row) in pc.NA_NOTES:
        assert broker in pc.BROKER_ORDER, broker
        assert row in row_labels, row
        assert (broker, row) not in filled, f"{broker}/{row} is not empty"


def test_state_exposes_the_grid_axes(fake_redis):
    state = pc.get_polling_knobs_state()
    assert [b["name"] for b in state["brokers"]] == list(pc.BROKER_ORDER)
    assert state["matrix_rows"] == pc._matrix_rows()
    assert len(state["na_notes"]) == len(pc.NA_NOTES)
    # Platform-wide sweeps carry no broker, so the page can table them apart.
    platform = [k for k in state["intervals"] if k["broker"] is None]
    assert {k["name"] for k in platform} == {
        k.name for k in pc.INT_KNOBS if k.broker is None
    }
