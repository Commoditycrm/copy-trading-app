"""Who may set and clear the stops on a position row.

A subscriber holds their own positions and must be able to protect them. The
row already let them arm a TRAILING stop and cancel open orders; set-stop and
cancel-stops were trader-only, so the same row answered "trader_only" for a
plain stop on the position they were already trailing.

What makes that safe is not the role but the account check inside each
endpoint — a broker account the caller does not own answers 404 — so these
pin both halves: the role gate is gone, and the ownership gate is not.
"""
import os
import sys
import uuid
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.deps import require_trader
from app.main import app


def _deps(path: str, method: str = "POST"):
    """Every dependency callable FastAPI resolves for one route."""
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            seen, stack = [], list(route.dependant.dependencies)
            while stack:
                d = stack.pop()
                seen.append(d.call)
                stack.extend(d.dependencies)
            return seen
    raise AssertionError(f"route not found: {method} {path}")


OWNER_SCOPED = [
    "/api/positions/{broker_symbol}/stop",
    "/api/positions/{broker_symbol}/stops/cancel",
    "/api/positions/{broker_symbol}/trailing-stop",
    "/api/positions/{broker_symbol}/cancel-open",
]


@pytest.mark.parametrize("path", OWNER_SCOPED)
def test_a_subscriber_is_not_locked_out_of_their_own_position(path):
    assert require_trader not in _deps(path), (
        f"{path} is trader-only; a subscriber cannot protect their own position"
    )


def test_the_stop_controls_all_agree_on_who_may_use_them():
    """The bug was divergence: trailing-stop allowed subscribers and set-stop
    did not, on the same row."""
    gates = {p: (require_trader in _deps(p)) for p in OWNER_SCOPED}
    assert len(set(gates.values())) == 1, f"stop controls disagree: {gates}"


def test_actions_that_touch_other_peoples_accounts_stay_trader_only():
    assert require_trader in _deps("/api/positions/close-all-subscribers")


def test_a_foreign_broker_account_is_refused(monkeypatch):
    """The real guard. Opening the endpoint to subscribers must not open it to
    OTHER people's positions."""
    from fastapi import HTTPException

    from app.api import positions as api

    mine, theirs = uuid.uuid4(), uuid.uuid4()
    acct = type("A", (), {"id": uuid.uuid4(), "user_id": theirs})()
    db = type("DB", (), {"get": staticmethod(lambda *a, **k: acct)})()
    user = type("U", (), {"id": mine})()

    with pytest.raises(HTTPException) as e:
        api.set_position_stop(
            "SPY261016P00100000", request=None, broker_account_id=acct.id,
            pnl_pct=Decimal("-25"), db=db, user=user,
        )
    assert e.value.status_code == 404


# ── the snapshot suite is a narrower gate than exiting ──────────────────────

SNAPSHOT_ROUTES = [
    ("/api/positions/snapshots", "GET"),
    ("/api/positions/snapshots/latest", "GET"),
    ("/api/positions/snapshots/today", "GET"),
    ("/api/positions/snapshots/{snapshot_id}", "DELETE"),
    ("/api/positions/snapshots/{snapshot_id}/positions/{index}", "DELETE"),
    ("/api/positions/re-enter", "POST"),
]


@pytest.mark.parametrize("path,method", SNAPSHOT_ROUTES)
def test_snapshot_routes_need_the_admin_allow_list(path, method):
    from app.api.deps import require_sell_all_access, require_snapshot_access
    deps = _deps(path, method)
    assert require_snapshot_access in deps, f"{path} is not on the snapshot gate"
    assert require_sell_all_access not in deps, (
        f"{path} would be reachable by a Discord trader's subscribers")


def test_exiting_your_own_positions_keeps_the_wider_gate():
    """Close-all is the half a Discord trader's subscribers must keep: the
    trader cannot exit their positions for them."""
    from app.api.deps import require_sell_all_access, require_snapshot_access
    deps = _deps("/api/positions/close-all")
    assert require_sell_all_access in deps and require_snapshot_access not in deps
