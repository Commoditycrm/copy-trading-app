"""A subscriber's ceiling on the whole mirrored order.

max_per_contract asks what ONE contract costs. This asks what the MIRROR costs,
which a per-contract cap cannot express: a subscriber on a 10x multiplier
mirroring a $50 contract passes a $500 per-contract cap and places a $500
order. The two are independent — a mirror can pass either and fail the other.
"""
import inspect
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.settings import SubscriberSettings
from app.schemas.settings import MaxPerOrderIn, SubscriberSettingsOut
from app.services import cache, copy_engine
from app.services.copy_engine import exceeds_order_cap, fit_to_order_cap, mirror_order_value
from app.services.discord_execution import order_value


# ── the value the gate compares ──────────────────────────────────────────────

def test_an_option_mirror_is_priced_per_contract_times_quantity():
    assert order_value(Decimal(4), Decimal("1.90"), True) == Decimal("760.00")


def test_a_stock_mirror_is_priced_without_the_multiplier_of_100():
    assert order_value(Decimal(100), Decimal("5.00"), False) == Decimal("500.00")


def test_the_multiplier_counts_toward_the_ceiling():
    """The gate is sized off the SCALED quantity, so a 10x subscriber cannot
    place 10x the ceiling from a cheap contract."""
    one = order_value(Decimal(1), Decimal("0.50"), True)
    ten = order_value(Decimal(10), Decimal("0.50"), True)
    assert one == Decimal("50.00") and ten == Decimal("500.00")


# ── the two caps are independent ─────────────────────────────────────────────

def test_a_cheap_contract_can_still_be_too_big_an_order():
    """$50 a contract passes a $500 per-contract cap; ten of them is a $500
    order that a $400 order cap must refuse."""
    per_contract = Decimal("0.50") * Decimal(100)
    total = order_value(Decimal(10), Decimal("0.50"), True)
    assert per_contract <= Decimal(500)
    assert total > Decimal(400)


def test_a_single_expensive_contract_can_pass_the_order_cap():
    """The mirror image: one $900 contract is a $900 order, inside a $1,000
    order cap but above a $500 per-contract cap."""
    per_contract = Decimal("9.00") * Decimal(100)
    total = order_value(Decimal(1), Decimal("9.00"), True)
    assert per_contract > Decimal(500)
    assert total <= Decimal(1000)


# ── the decision itself ──────────────────────────────────────────────────────

def _skips(px, qty, is_option, cap):
    return exceeds_order_cap(mirror_order_value(px, qty, is_option), cap)


def test_a_mirror_over_the_ceiling_is_skipped():
    assert _skips(Decimal("1.90"), 6, True, Decimal(1000)) is True     # $1,140


def test_a_mirror_under_the_ceiling_copies():
    assert _skips(Decimal("1.90"), 4, True, Decimal(1000)) is False    # $760


def test_a_mirror_exactly_on_the_ceiling_copies():
    """Strictly greater refuses, so the boundary itself trades."""
    assert _skips(Decimal("1.90"), 5, True, Decimal("950")) is False   # $950


def test_a_stock_mirror_is_gated_too():
    """Unlike the per-contract cap, which has no meaning on a stock."""
    assert _skips(Decimal("5.00"), 100, False, Decimal(400)) is True   # $500


def test_no_ceiling_never_skips():
    assert _skips(Decimal("1.90"), 1000, True, None) is False


def test_an_unpriced_mirror_is_not_treated_as_free():
    """A mirror we cannot price must not read as $0 — that would pass
    everything the ceiling cannot see rather than nothing."""
    assert mirror_order_value(None, 4, True) is None
    assert _skips(None, 4, True, Decimal(1)) is False


def test_a_zero_quantity_is_not_priced():
    assert mirror_order_value(Decimal("1.90"), 0, True) is None


# ── an over-cap entry is cut to fit, not skipped ─────────────────────────────

def test_an_over_cap_option_entry_is_cut_to_what_fits():
    """6 x $1.90 = $1,140 against $1,000: five contracts ($950) fit."""
    assert fit_to_order_cap(6, Decimal("1.90"), True, Decimal(1000)) == 5


def test_an_over_cap_stock_entry_is_cut_to_what_fits():
    """100 shares at $5 = $500 against $400: 80 shares."""
    assert fit_to_order_cap(100, Decimal("5.00"), False, Decimal(400)) == 80


def test_an_entry_under_the_cap_keeps_its_size():
    assert fit_to_order_cap(4, Decimal("1.90"), True, Decimal(1000)) == 4


def test_an_entry_exactly_on_the_cap_keeps_its_size():
    assert fit_to_order_cap(5, Decimal("1.90"), True, Decimal("950")) == 5


def test_nothing_fits_when_one_contract_is_over_the_cap():
    """$1,200 a contract against $1,000: zero, which the fanout skips."""
    assert fit_to_order_cap(3, Decimal("12.00"), True, Decimal(1000)) == 0


def test_no_cap_or_no_price_leaves_the_size_alone():
    assert fit_to_order_cap(10, Decimal("1.90"), True, None) == 10
    assert fit_to_order_cap(10, None, True, Decimal(100)) == 10


def test_the_fanout_fits_rather_than_skips():
    src = inspect.getsource(copy_engine.fanout_async)
    gate = src[src.index("Max per-ORDER value gate"):]
    assert "fit_to_order_cap(" in gate
    assert "copy.fitted_max_per_order" in gate
    # A cut-down fresh entry locks the position at the size it got, so the
    # trader's partial trims scale against what the subscriber holds.
    assert "scale_mult = (_fit /" in gate


# ── a market order is priced at the market, once ─────────────────────────────

class _Order:
    def __init__(self, order_type, limit=None, fill=None, option=True):
        from app.models.order import InstrumentType, OrderType
        self.order_type = OrderType(order_type)
        self.limit_price = limit
        self.filled_avg_price = fill
        self.instrument_type = InstrumentType.OPTION if option else InstrumentType.STOCK
        self.symbol = "SPY"
        self.option_strike = Decimal(600) if option else None
        self.option_right = None
        self.option_expiry = None


def test_a_limit_order_is_priced_at_its_limit(monkeypatch):
    monkeypatch.setattr(copy_engine, "_live_option_premium", lambda *a: Decimal("9.99"))
    px = copy_engine._entry_gate_price(None, None, _Order("limit", limit=Decimal("1.50")))
    assert px == Decimal("1.50")


def test_a_market_option_is_priced_at_the_live_ask_not_the_fill(monkeypatch):
    monkeypatch.setattr(copy_engine, "_live_option_premium", lambda *a: Decimal("2.10"))
    px = copy_engine._entry_gate_price(None, None, _Order("market", fill=Decimal("1.50")))
    assert px == Decimal("2.10")


def test_a_market_stock_is_priced_at_the_live_mark(monkeypatch):
    from app.services import live_marks
    monkeypatch.setattr(live_marks, "contract_mark", lambda *a, **k: Decimal("101.25"))
    px = copy_engine._entry_gate_price(None, None, _Order("market", option=False))
    assert px == Decimal("101.25")


def test_with_no_live_price_the_trader_fill_is_used(monkeypatch):
    from app.services import live_marks
    monkeypatch.setattr(copy_engine, "_live_option_premium", lambda *a: None)
    monkeypatch.setattr(live_marks, "contract_mark", lambda *a, **k: None)
    px = copy_engine._entry_gate_price(None, None, _Order("market", fill=Decimal("1.50")))
    assert px == Decimal("1.50")


# ── it is actually wired into the fanout ─────────────────────────────────────

def test_the_fanout_gates_on_the_order_cap():
    src = inspect.getsource(copy_engine.fanout_async)
    assert "skipped_max_per_order" in src
    assert "_fresh_order_caps" in src


def test_the_gate_reads_the_cap_from_the_database_not_the_cache():
    """A risk cap has to apply on the very NEXT trade. The cached subscriber
    can lag by the cache TTL — which is how an over-cap option open slipped
    through in QA and why the per-contract gate re-reads it."""
    src = inspect.getsource(copy_engine.fanout_async)
    assert "SubscriberSettings.max_per_order" in src


def test_a_close_is_never_gated():
    """Subscribers must always be able to exit, whatever it is now worth."""
    src = inspect.getsource(copy_engine.fanout_async)
    gate = src[src.index("Max per-ORDER value gate"):]
    assert "if not is_closing_effective" in gate[:1500]


# ── plumbing ─────────────────────────────────────────────────────────────────

def test_the_column_exists_and_is_optional():
    col = SubscriberSettings.__table__.c.max_per_order
    assert col.nullable, "NULL is how a subscriber says 'no cap'"


def test_the_settings_response_carries_it():
    assert "max_per_order" in SubscriberSettingsOut.model_fields


def test_zero_is_rejected():
    """A $0 ceiling would skip every copy rather than meaning 'no ceiling' —
    the same reasoning as MaxPerContractIn."""
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        MaxPerOrderIn(max_per_order=Decimal(0))
    assert MaxPerOrderIn(max_per_order=None).max_per_order is None


def test_it_round_trips_through_the_fanout_cache():
    assert "max_per_order" in cache.CachedSubscriber.__dataclass_fields__
    d = cache._sub_to_dict(_FakeSub(Decimal("750.00")))
    assert cache._sub_from_dict(d).max_per_order == Decimal("750.00")


class _FakeSub:
    def __init__(self, cap):
        import uuid
        self.user_id = uuid.uuid4()
        self.following_trader_id = uuid.uuid4()
        self.copy_enabled = True
        self.multiplier = Decimal(1)
        self.daily_loss_limit = None
        self.daily_profit_limit = None
        self.pnl_auto_paused_at = None
        self.symbol_exclusion_list = []
        self.symbol_inclusion_list = []
        self.copy_trader_bracket = False
        self.eod_autoclose_enabled = False
        self.eod_autoclose_minutes = 15
        self.max_per_contract = None
        self.max_per_order = cap
