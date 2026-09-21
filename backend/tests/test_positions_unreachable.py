"""A failed broker read must not render as "you hold nothing".

THE BUG
-------
``list_positions`` caught per-account failures, skipped the account, and
returned 200 with it simply absent. "This account is flat" and "we could not
reach this broker" were byte-identical responses, so the UI had no way to tell
them apart and drew an empty table.

That is what subscribers saw on prod (2026-09-21): Webull answered 429 to a
position read, the account was dropped, and the positions page showed nothing
while they held real positions. Refresh, the read succeeded, positions
reappeared — the flapping they reported.

AND WHY THE 429 HAPPENED
------------------------
Not volume. Rejections landed 32ms and 129ms apart at a whole-platform peak
under 50 requests/min across every user and broker, against a documented
300/min. Webull refuses SIMULTANEOUS reads of /openapi/assets/positions for one
account — and several independent readers exist (the positions table fires four
staggered refreshes per order event; the calendar fetches live unrealized on its
own). So display reads are now coalesced behind a per-account lock + short TTL.

These tests pin both halves, plus the boundary that keeps the coalescing safe:
decision paths must never get a cached position read.
"""
import os
import sys
import threading
import time
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.brokers import webull as wb
from app.brokers.base import BrokerPosition
from app.models.order import InstrumentType


def _adapter(app_key="k1", account_id="a1"):
    a = wb.WebullAdapter.__new__(wb.WebullAdapter)
    a.app_key, a.app_secret = app_key, "s"
    a.account_id, a.region_id = account_id, "us"
    return a


def _pos(sym="NIO", qty="2"):
    return BrokerPosition(
        broker_symbol=sym, symbol=sym, instrument_type=InstrumentType.STOCK,
        quantity=Decimal(qty), avg_entry_price=None, current_price=None,
        market_value=None, unrealized_pnl=None,
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    wb._positions_cache.clear()
    wb._positions_locks.clear()
    yield
    wb._positions_cache.clear()
    wb._positions_locks.clear()


# ── coalescing ──────────────────────────────────────────────────────────────
def test_concurrent_display_reads_make_one_broker_call():
    """The actual fix. Two readers hitting the same account at the same instant
    is what Webull 429s — so only ONE request may leave."""
    a = _adapter()
    calls: list[float] = []
    started = threading.Event()

    def _slow_fetch():
        calls.append(time.monotonic())
        started.set()
        time.sleep(0.25)          # hold the lock like a real HTTP round trip
        return [_pos()]
    a._fetch_positions = _slow_fetch

    results: list = []
    threads = [
        threading.Thread(target=lambda: results.append(a.get_positions(cached_ok=True)))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
        started.wait(timeout=1.0)  # ensure the first is in-flight before the rest
    for t in threads:
        t.join(timeout=5.0)

    assert len(calls) == 1, f"{len(calls)} broker calls — concurrent reads not coalesced"
    assert len(results) == 5
    assert all(r[0].symbol == "NIO" for r in results)


def test_cache_expires_so_positions_do_not_go_stale():
    a = _adapter()
    n = {"c": 0}
    a._fetch_positions = lambda: (n.__setitem__("c", n["c"] + 1), [_pos()])[1]

    a.get_positions(cached_ok=True)
    a.get_positions(cached_ok=True)
    assert n["c"] == 1, "second read inside the TTL should reuse the first"

    wb._positions_cache[f"{a.app_key}:{a.account_id}"] = (
        time.monotonic() - wb._POSITIONS_TTL_S - 0.1, [_pos()]
    )
    a.get_positions(cached_ok=True)
    assert n["c"] == 2, "an expired entry must be refetched"


def test_accounts_do_not_share_a_cache_entry():
    """Two accounts under one app_key must never see each other's positions."""
    a1, a2 = _adapter(account_id="a1"), _adapter(account_id="a2")
    a1._fetch_positions = lambda: [_pos("NIO")]
    a2._fetch_positions = lambda: [_pos("TSLA")]
    assert a1.get_positions(cached_ok=True)[0].symbol == "NIO"
    assert a2.get_positions(cached_ok=True)[0].symbol == "TSLA"


# ── the boundary that keeps it safe ─────────────────────────────────────────
def test_decision_paths_always_read_live():
    """auto_liquidator / position_enforcer / order_retry decide whether to PLACE
    an order from this read. A stale snapshot could close a position that is
    already closed, so the DEFAULT must bypass the cache entirely."""
    a = _adapter()
    n = {"c": 0}
    a._fetch_positions = lambda: (n.__setitem__("c", n["c"] + 1), [_pos()])[1]

    a.get_positions(cached_ok=True)      # populate
    for _ in range(3):
        a.get_positions()                # default — must ignore the cache
    assert n["c"] == 4, "uncached reads must always hit the broker"


def test_a_failed_read_is_not_cached():
    """Caching an exception would turn one throttle into TTL seconds of
    failure for every reader. The lock still serialises them, which is the
    part that prevents the 429."""
    a = _adapter()
    n = {"c": 0}

    def _boom():
        n["c"] += 1
        raise RuntimeError("HTTP Status: 429, Code: TOO_MANY_REQUESTS")
    a._fetch_positions = _boom

    for _ in range(2):
        with pytest.raises(RuntimeError):
            a.get_positions(cached_ok=True)
    assert n["c"] == 2
    assert f"{a.app_key}:{a.account_id}" not in wb._positions_cache


def test_invalidate_drops_the_entry():
    """After a fill or a close the position changed; the next display read must
    not serve the pre-trade snapshot."""
    a = _adapter()
    a._fetch_positions = lambda: [_pos()]
    a.get_positions(cached_ok=True)
    assert f"{a.app_key}:{a.account_id}" in wb._positions_cache
    wb.invalidate_positions_cache(a.app_key, a.account_id)
    assert f"{a.app_key}:{a.account_id}" not in wb._positions_cache


# ── the user-facing half ────────────────────────────────────────────────────
def test_unreachable_detail_is_user_safe_and_specific():
    """The reason shown to a user must never be the raw exception — it carries
    account ids and request ids. Throttling reads as transient because it is."""
    from app.api.positions import _unreachable_detail

    thr = _unreachable_detail(RuntimeError(
        "HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: Too many requests, "
        "RequestID: 9373c326-a147-42d5-8132-01af52bb9013"
    ))
    assert "retry" in thr.lower()
    assert "9373c326" not in thr, "must not leak the request id"
    assert "429" not in thr, "must not leak the status code"

    assert "reconnect" in _unreachable_detail(RuntimeError("HTTP 401 unauthorized")).lower()
    assert "timed out" in _unreachable_detail(RuntimeError("read timeout")).lower()
    assert _unreachable_detail(RuntimeError("something odd")) == "Broker unavailable — retrying"


def test_payload_shape_separates_empty_from_unreachable():
    """The distinction the whole change exists for: an account that returned
    nothing and an account we could not ask must not look the same."""
    from app.schemas.position import PositionsPayload, UnreachableAccount

    flat = PositionsPayload(positions=[], unreachable=[])
    broken = PositionsPayload(positions=[], unreachable=[UnreachableAccount(
        broker_account_id=uuid.uuid4(), broker="webull", label="Webull",
        detail="Rate limited by the broker — retrying",
    )])
    assert flat.positions == broken.positions == []
    assert not flat.unreachable and broken.unreachable, "these must be distinguishable"


# ── the default response shape must not change ──────────────────────────────
class _StubDB:
    def __init__(self, accts): self._accts = accts
    def execute(self, *_a, **_k): return self
    def scalars(self): return self
    def all(self): return self._accts


class _StubAcct:
    def __init__(self, broker="webull", ok=True):
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.label = "My Webull"
        self.encrypted_credentials = "x"
        self.connection_status = "connected"
        self.ok = ok
        class _B:  # noqa: N801
            value = broker
        self.broker = _B()


def _call_list_positions(accts, detail):
    """Drive the endpoint function directly with stubs — no DB, no network."""
    from app.api import positions as mod

    saved_adapter, saved_decrypt = mod.adapter_for, mod.decrypt_json

    class _Ad:
        def __init__(self, ok): self.ok = ok
        def get_positions(self, *, cached_ok=False):
            if not self.ok:
                raise RuntimeError("HTTP Status: 429, Code: TOO_MANY_REQUESTS")
            return [_pos()]

    by_id = {a.id: a for a in accts}
    mod.decrypt_json = lambda _b: {}
    mod.adapter_for = lambda acct, _c: _Ad(by_id[acct.id].ok)
    try:
        return mod.list_positions(
            db=_StubDB(accts), user=type("U", (), {"id": uuid.uuid4()})(), detail=detail
        )
    finally:
        mod.adapter_for, mod.decrypt_json = saved_adapter, saved_decrypt


def test_default_shape_is_still_a_bare_list():
    """trades/page.tsx (twice) and BulkExitBar all call /api/positions and expect
    a LIST. Adding the detail form must not change what they receive."""
    got = _call_list_positions([_StubAcct(ok=True)], detail=False)
    assert isinstance(got, list)
    assert got and got[0].symbol == "NIO"


def test_a_failing_broker_still_does_not_blank_the_others():
    """The original intent survives: one bad broker must not take the list down."""
    good, bad = _StubAcct(ok=True), _StubAcct(ok=False)
    payload = _call_list_positions([good, bad], detail=True)
    assert len(payload.positions) == 1, "the healthy account's positions survive"
    assert len(payload.unreachable) == 1
    assert payload.unreachable[0].broker_account_id == bad.id, \
        "the FAILING account must be the one reported, not the healthy one"
    assert "retry" in payload.unreachable[0].detail.lower()


def test_detail_form_reports_the_failed_account():
    payload = _call_list_positions([_StubAcct(ok=False)], detail=True)
    assert payload.positions == []
    assert len(payload.unreachable) == 1
    u = payload.unreachable[0]
    assert u.broker == "webull" and u.label == "My Webull"
    assert "TOO_MANY_REQUESTS" not in u.detail, "raw broker text must not reach the UI"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        wb._positions_cache.clear(); wb._positions_locks.clear()
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
