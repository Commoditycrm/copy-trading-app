"""Unit tests for the direct-Webull listener's safety guards.

No SDK, no DB, no network — pure logic:
  * feature flags default to the SAFE state (off / shadow),
  * the generation guard drops events from a superseded listener (so a
    lingering gRPC thread after a restart can never fan out), and
  * shadow mode is a pure log — it never touches the DB or fanout.

Run standalone:  .venv/bin/python tests/test_webull_listener.py
Or under pytest: pytest tests/test_webull_listener.py
"""
import math
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.webull_listener as wl
from app.config import get_settings
from app.models.order import OrderSide, OrderStatus

_PAYLOAD = {
    "symbol": "APP", "side": "BUY", "order_status": "FILLED",
    "category": "US_OPTION", "order_id": "D5G4DKBS", "filled_qty": "1.00",
    "filled_price": "4.50", "filled_time": "2026-08-06T14:04:46.424+0000",
}


def test_flags_default_safe():
    # Assert the CODE defaults are safe (a local .env may override at runtime,
    # so check the field defaults on the Settings class, not get_settings()).
    from app.config import Settings
    assert Settings.model_fields["webull_direct_enabled"].default is False, \
        "direct Webull must default OFF in code"
    assert Settings.model_fields["webull_direct_shadow_mode"].default is True, \
        "shadow mode must default ON in code"


def test_generation_guard_drops_stale_events():
    """An event tagged with an OLD generation (a superseded listener) is dropped
    before any shadow-log or fanout — the guard against double-mirroring after a
    listener restart."""
    tid = uuid.uuid4()
    wl._generation[tid] = 5
    # Stale generation 3 != current 5 → must return immediately, no raise.
    wl._on_order_event(tid, uuid.uuid4(), 3, {}, _PAYLOAD)   # should be a no-op


def test_current_generation_shadow_is_pure_log():
    """With the current generation and shadow mode (default ON), the handler
    logs and returns without touching the DB or fanout — no exception."""
    tid = uuid.uuid4()
    wl._generation[tid] = 1
    wl._on_order_event(tid, uuid.uuid4(), 1, {}, _PAYLOAD)   # shadow path: log-only


def test_non_dict_payload_is_ignored():
    tid = uuid.uuid4()
    wl._generation[tid] = 1
    wl._on_order_event(tid, uuid.uuid4(), 1, {}, "not-a-dict")   # must not raise


def test_rest_order_to_payload_stock():
    """A REST today-orders stock row flattens into the same payload shape the
    gRPC handler consumes (leg detail from items[0], ids from the wrapper)."""
    row = {
        "items": [{"symbol": "NIO", "category": "US_STOCK", "filled_price": "4.5600",
                   "filled_qty": "1", "last_filled_time": "2026-08-06 16:47:17.816+0000",
                   "order_status": "FILLED", "order_type": "LIMIT", "qty": "1",
                   "side": "SELL", "limit_price": "4.560"}],
        "client_order_id": "coid", "order_id": "OID123",
        "account_id": "ACC", "order_type": "LMT",
    }
    p = wl._rest_order_to_payload(row)
    assert p["order_id"] == "OID123"
    assert p["symbol"] == "NIO" and p["side"] == "SELL"
    assert p["category"] == "US_STOCK"
    assert p["order_status"] == "FILLED"
    # space separator normalised to 'T' so the ISO parser accepts it
    assert p["filled_time"] == "2026-08-06T16:47:17.816+0000"
    assert wl._map_status(p["order_status"]) == OrderStatus.FILLED
    assert wl._map_side(p["side"]) == OrderSide.SELL


def test_rest_order_to_payload_option_and_no_id():
    opt = {"items": [{"symbol": "OPRA", "category": "US_OPTION", "order_status": "FILLED",
                      "side": "SELL", "qty": "1", "order_type": "MARKET"}],
           "order_id": "OPT1", "client_order_id": "c", "account_id": "A"}
    p = wl._rest_order_to_payload(opt)
    assert p["category"].upper() == "US_OPTION"
    # a row without an order_id is dropped (can't dedup/persist it)
    assert wl._rest_order_to_payload({"items": [{"symbol": "X"}]}) is None


def test_parse_wb_time_both_formats():
    """Both Webull timestamp forms parse to an aware UTC datetime."""
    a = wl._parse_wb_time("2026-08-06T14:04:46.424+0000")   # stream (ISO 'T')
    b = wl._parse_wb_time("2026-08-06 16:47:17.816+0000")   # REST (space)
    assert a is not None and a.utcoffset().total_seconds() == 0
    assert b is not None and b.hour == 16 and b.minute == 47
    assert wl._parse_wb_time(None) is None
    assert wl._parse_wb_time("garbage") is None


def test_safe_poll_interval_respects_rate_limit():
    """Webull limits the order-query endpoints to 2 requests per 2 SECONDS.

    It is the window that bites, not the average: evenly spaced calls a gap `g`
    apart put floor(2/g)+1 of them inside some 2s window, so g must be strictly
    greater than 1.0s or three land in one window and the last 429s. That is the
    shape behind the prod incident where the 3rd account of each burst failed
    every cycle.

    The cap used to be documented here as 10 req/30s shared across endpoints.
    It is neither shared nor that small -- each endpoint keeps its own counter
    and 2/2s is 1 call/s sustained -- so the old interval was 3x slower than it
    needed to be, for nothing.
    """
    WINDOW_S, WINDOW_CAP = 2.0, 2

    for n in (1, 2, 3, 5, 10):
        interval = wl._safe_poll_interval(n)
        gap = interval / n
        assert gap > 1.0, (n, gap)
        in_window = math.floor(WINDOW_S / gap) + 1
        assert in_window <= WINDOW_CAP, (n, gap, in_window)

    # and it must not have become gratuitously slow in the other direction
    assert wl._safe_poll_interval(1) <= 6.0


def test_order_fingerprint_catches_modify():
    """The poll fingerprint changes on a MODIFY (price/qty edit) even when the
    status is unchanged — otherwise the poller would skip modifications."""
    base = {"order_status": "PENDING", "order_type": "LIMIT", "qty": "1",
            "limit_price": "3.80", "stop_price": None, "filled_qty": "0",
            "filled_price": None}
    fp0 = wl._order_fingerprint(base)
    # same order, re-seen unchanged → same fingerprint (poller skips)
    assert wl._order_fingerprint(dict(base)) == fp0
    # price modified, status still PENDING → fingerprint MUST differ
    assert wl._order_fingerprint({**base, "limit_price": "3.95"}) != fp0
    # qty modified → differs
    assert wl._order_fingerprint({**base, "qty": "2"}) != fp0
    # incremental fill within a working order → differs
    assert wl._order_fingerprint({**base, "filled_qty": "1"}) != fp0


def test_public_interface_matches_other_listeners():
    for name in ("bind_loop", "start_all_listeners", "start_listener", "stop_listener",
                 "stop_all_listeners", "has_running_listener", "running_trader_ids"):
        assert hasattr(wl, name), f"missing public function {name}"
    assert isinstance(wl._tasks, dict)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\nAll webull-listener guard tests passed.")


def test_the_day_orders_page_covers_a_busy_session():
    """30 was enough for a normal day and silently was not for a busy one: one
    account placed 47 orders in an afternoon of testing, so everything past the
    page's edge was never seen by the poller -- its fills and cancels never
    reached the order history. A bigger page costs the SAME single request, so
    no rate-limit budget is spent widening it."""
    import inspect

    from app.services import webull_listener as wl

    assert wl._DAYORDERS_PAGE_SIZE >= 100
    default = inspect.signature(wl._list_today_orders).parameters["page_size"].default
    assert default == wl._DAYORDERS_PAGE_SIZE, (
        "the poller must use the widened page, not its own literal"
    )


# ── option-vs-stock detection: the NIO misclassification ─────────────────────
#
# Live 2026-09-25: a NIO option placed in the Webull app was persisted as
# instrument_type=STOCK with no strike/right/expiry (order VIG6FSBI6BA0FC...).
# Two consequences, and the second is the one that matters:
#   * order history could only render "NIO"; and
#   * _persist_and_fanout's "refuse to mirror an option whose contract we
#     cannot resolve" guard is keyed off this flag, so instead of being held
#     back the order went to fanout as a $0.20 NIO STOCK buy.


@pytest.mark.parametrize("payload, why", [
    ({"symbol": "NIO", "category": "US_OPTION"}, "the documented spelling"),
    ({"symbol": "NIO", "category": "OPTION"}, "the bare spelling, also live"),
    ({"symbol": "NIO", "category": "us_option"}, "lower case"),
    ({"symbol": "NIO", "instrument_type": "US_OPTION"}, "type on instrument_type"),
    ({"symbol": "NIO", "combo_ticker_type": "OPTION"}, "type on combo_ticker_type"),
    ({"symbol": "NIO", "asset_type": "OPTION"}, "type on asset_type"),
    ({"symbol": "NIO250925C00003500"}, "no type field at all — OCC symbol"),
    ({"symbol": "NIO  250925C00003500"}, "OCC with Webull's padding"),
])
def test_every_spelling_of_option_is_recognised(payload, why):
    assert wl._is_option_payload(payload) is True, why


@pytest.mark.parametrize("payload", [
    {"symbol": "NIO", "category": "US_STOCK"},
    {"symbol": "AAPL", "category": "STOCK"},
    {"symbol": "NIO"},                      # plain ticker, nothing to go on
    {},
])
def test_a_stock_is_not_promoted_to_an_option(payload):
    """The widening must not run the other way: calling a stock an option
    would send it down the contract-resolution path and refuse to mirror it."""
    assert wl._is_option_payload(payload) is False


def test_the_exact_match_that_caused_it_is_gone():
    """Pins the actual defect, not just the behaviour around it. The old line
    was `== "US_OPTION"` on `category` alone."""
    import inspect
    src = inspect.getsource(wl._persist_and_fanout)
    assert '== "US_OPTION"' not in src
    assert "_is_option_payload(payload)" in src


def test_the_rest_flattening_looks_at_the_same_keys():
    """The poll path builds its own payload, so widening the detector alone
    would leave the poll still blind to a type on instrument_type."""
    p = wl._rest_order_to_payload(
        {"order_id": "X", "items": [{"symbol": "NIO", "instrument_type": "OPTION"}]}
    )
    assert wl._is_option_payload(p) is True


def test_an_option_that_cannot_be_resolved_is_never_fanned_out_as_stock(monkeypatch):
    """The dangerous half. Misreading the type skipped this refusal entirely —
    which is how a NIO OPTION reached fanout as a NIO STOCK buy at $0.20."""
    import inspect
    src = inspect.getsource(wl._persist_and_fanout)
    resolve_at = src.index("_resolve_option_contract")
    guard = src[resolve_at:resolve_at + 700]
    # The refusal must RETURN — logging it and carrying on would still persist
    # a stock row and still fan it out.
    assert "if resolved is None:" in guard
    assert "return" in guard.split("if resolved is None:")[1][:400]


def test_the_wrapper_beats_a_wrong_leg_category():
    """The live failure, replayed from Webull's actual response. Webull
    contradicts itself inside ONE order row and puts the wrong field first:

        items[0].category = "US_STOCK"     <- plainly wrong
        combo_ticker_type = "PUT_OPTION"   <- the truth

    (order RDGG4OSLPLGU..., a real NIO $4 PUT). A first-non-empty read stops
    at the leg and never sees the wrapper."""
    row = {
        "order_id": "RDGG4OSLPLGU42HGTKJ8DEOQBB",
        "combo_ticker_type": "PUT_OPTION",
        "items": [{"symbol": "NIO", "category": "US_STOCK", "side": "BUY",
                   "order_status": "SUBMITTED", "qty": "1", "limit_price": "0.2"}],
    }
    p = wl._rest_order_to_payload(row)
    assert p["category"] == "US_STOCK"          # passed through, not collapsed
    assert p["combo_ticker_type"] == "PUT_OPTION"
    assert wl._is_option_payload(p) is True


@pytest.mark.parametrize("payload, why", [
    ({"symbol": "NIO", "category": "US_STOCK", "combo_ticker_type": "PUT_OPTION"},
     "wrapper says option, leg says stock"),
    ({"symbol": "NIO", "category": "US_STOCK", "combo_ticker_type": "CALL_OPTION"},
     "the call side of the same shape"),
    ({"symbol": "NIO", "category": "US_STOCK", "instrument_type": "OPTION"},
     "type on instrument_type while category lies"),
    ({"symbol": "NIO", "category": "US_STOCK", "strike_price": "4.00"},
     "a strike is proof regardless of the type field"),
    ({"symbol": "NIO", "category": "US_STOCK", "option_expire_date": "2026-09-25"},
     "so is an expiry"),
])
def test_any_field_naming_an_option_wins(payload, why):
    """Not the first non-empty one — that is the whole defect."""
    assert wl._is_option_payload(payload) is True, why


def test_a_genuine_stock_is_still_a_stock():
    """The widening must not swallow real stock orders: every field agrees,
    and there are no contract terms."""
    assert wl._is_option_payload(
        {"symbol": "NIO", "category": "US_STOCK", "combo_ticker_type": "NORMAL"}
    ) is False


def test_the_detector_does_not_stop_at_the_first_key():
    """Pins the mechanism. _first() short-circuits, which is exactly how the
    wrapper's verdict got hidden behind the leg's wrong one."""
    import inspect
    src = inspect.getsource(wl._is_option_payload)
    assert "_first(" not in src


# ── the id a later cancel needs ──────────────────────────────────────────────
#
# Every Webull order endpoint takes the CLIENT order id — cancel_order,
# cancel_option, get_order_detail, replace_order — and WebullAdapter is built
# on that: place_order returns our client_order_id AS broker_order_id so
# cancel/replace/read keep working. The listener stored Webull's own order_id
# instead, so an order placed in the Webull app could never be cancelled from
# Kopyya. Confirmed live 2026-09-25 on order EKIOD4IHFID9CRICBH2K5J5NBA:
#   get_order_detail(account, order_id)        -> 417 "Order not present"
#   get_order_detail(account, client_order_id) -> 200

def test_the_stored_handle_is_the_client_order_id():
    """Pins the mechanism at the source: the INSERT must use the client id."""
    import inspect
    src = inspect.getsource(wl._persist_and_fanout)
    assert "broker_order_id=cancel_handle," in src
    assert "broker_order_id=broker_order_id," not in src
    # And the handle must prefer the client id, falling back only when absent.
    assert "cancel_handle = client_oid or broker_order_id" in src


def test_a_row_stored_under_the_wrong_id_is_repointed():
    """Rows written before the fix hold an id no Webull endpoint accepts, so
    Cancel on them fails forever. The feed carries both ids — correct it."""
    import inspect
    src = inspect.getsource(wl._persist_and_fanout)
    heal = src[src.index("if existing is not None:"):][:900]
    assert "existing.broker_order_id != client_oid" in heal
    assert "existing.broker_order_id = client_oid" in heal


def test_either_id_still_finds_the_row():
    """The heal and the dedup both depend on this: an event carrying Webull's
    order_id must still match a row now stored under the client id, or the
    listener would insert a duplicate instead of updating."""
    import inspect
    src = inspect.getsource(wl.find_placed_order)
    assert "or_(" in src                      # matched on EITHER id
    assert 'i.replace("-", "")' in src        # and both dash spellings
