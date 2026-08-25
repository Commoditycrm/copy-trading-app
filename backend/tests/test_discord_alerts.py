"""Tests for the Discord trade-alert cards (Phase 2).

Covers the pure card formatter (ENTERING / TRIMMING / CLOSING), the FIFO
round-trip P&L reconstruction (``_round_trip_summary``), and the entry-vs-close
classifier fallback (``_is_closing``), which must work off the trader's OWN
prior fills because the Alpaca listener always stores is_closing=False.
"""
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.services.discord_alerts as da
from app.models.order import (
    InstrumentType,
    Order,
    OrderSide,
    OptionRight,
    OrderStatus,
    OrderType,
)

_EXP = date(2026, 8, 10)
_T0 = datetime(2026, 8, 10, 16, 0, 0, tzinfo=timezone.utc)


def _session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    Order.__table__.create(eng)  # only the table under test
    return Session(eng)


def _opt(side, price, qty="1", *, closing=False, at=_T0, uid=None):
    return Order(
        id=uuid.uuid4(),
        user_id=uid or uuid.uuid4(),
        broker_account_id=uuid.uuid4(),
        instrument_type=InstrumentType.OPTION,
        symbol="AMD",
        option_strike=Decimal("465"),
        option_right=OptionRight.PUT,
        option_expiry=_EXP,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Decimal(qty),
        is_closing=closing,
        status=OrderStatus.FILLED,
        filled_quantity=Decimal(qty),
        filled_avg_price=Decimal(str(price)),
        created_at=at,
    )


def _stock(side, price, qty="200", *, at=_T0, uid=None):
    return Order(
        id=uuid.uuid4(),
        user_id=uid or uuid.uuid4(),
        broker_account_id=uuid.uuid4(),
        instrument_type=InstrumentType.STOCK,
        symbol="RDGT",
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
        is_closing=False,
        status=OrderStatus.FILLED,
        filled_quantity=Decimal(qty),
        filled_avg_price=Decimal(str(price)),
        created_at=at,
    )


# ── card formatter ────────────────────────────────────────────────────────────

def test_card_entering_option():
    e = da.build_card(_opt(OrderSide.BUY, "2.44"), {"kind": "enter"})["embeds"][0]
    assert e["title"] == "🟢 ENTERING · AMD $465 PUT · 08/10"
    assert e["description"] == "1 @ $2.44 · $244"   # 2.44 × 1 × 100
    assert e["color"] == da._COLOR_ENTER


def test_card_entering_default_summary_none():
    """No summary → ENTERING (safe default)."""
    e = da.build_card(_opt(OrderSide.BUY, "2.44"))["embeds"][0]
    assert e["title"].startswith("🟢 ENTERING")


def test_card_trimming_option():
    s = {"kind": "trim", "realized": Decimal("13"), "pct": 5.0,
         "remaining": Decimal("1"), "original": Decimal("2")}
    e = da.build_card(_opt(OrderSide.SELL, "2.85"), s)["embeds"][0]
    assert e["title"] == "🟡 TRIMMING · AMD $465 PUT · 08/10"
    assert e["description"] == "Sold 1 @ $2.85 · +$13 · +5%\n1 of 2 still open"
    assert e["color"] == da._COLOR_TRIM


def test_card_closing_option_with_pnl_and_total():
    s = {"kind": "close", "realized": Decimal("18"), "pct": 7.0,
         "total_realized": Decimal("31"), "total_pct": 6.0}
    e = da.build_card(_opt(OrderSide.SELL, "2.90"), s)["embeds"][0]
    assert e["title"] == "🔴 CLOSING · AMD $465 PUT · 08/10"
    assert e["description"] == "Sold 1 @ $2.90 · +$18 · +7%\nPosition closed · total +$31 · +6%"
    assert e["color"] == da._COLOR_CLOSE


def test_card_closing_negative_pnl_signs():
    s = {"kind": "close", "realized": Decimal("-157"), "pct": -64.0,
         "total_realized": Decimal("-157"), "total_pct": -64.0}
    e = da.build_card(_opt(OrderSide.SELL, "0.87"), s)["embeds"][0]
    assert "Sold 1 @ $0.87 · -$157 · -64%" in e["description"]
    assert "total -$157 · -64%" in e["description"]


def test_card_entering_stock_notional_no_x100():
    e = da.build_card(_stock(OrderSide.BUY, "1.53"), {"kind": "enter"})["embeds"][0]
    assert e["title"] == "🟢 ENTERING · RDGT"
    assert e["description"] == "200 @ $1.53 · $306"  # stock: no ×100


def test_money_and_pnl_formatting():
    assert da._fmt_money(Decimal("465")) == "$465"
    assert da._fmt_money(Decimal("1240")) == "$1,240"
    assert da._fmt_money(Decimal("2.44")) == "$2.44"
    assert da._fmt_money(None) == "—"
    assert da._fmt_signed(Decimal("13")) == "+$13"
    assert da._fmt_signed(Decimal("-5")) == "-$5"
    assert da._fmt_signed(Decimal("1240.50")) == "+$1,240.50"
    assert da._fmt_pct(5.0) == "+5%"
    assert da._fmt_pct(-64.0) == "-64%"
    assert da._fmt_pct(None) == ""


# ── FIFO round-trip P&L (_round_trip_summary) ────────────────────────────────

def _rt(side, price, qty, at, uid):
    return _opt(side, price, qty=str(qty), at=at, uid=uid)


def test_round_trip_enter_trim_close():
    """The image example: BUY 2@2.72 → TRIM 1@2.85 (+$13,+5%,1of2) →
    CLOSE 1@2.90 (+$18,+7%, total +$31,+6%)."""
    db = _session(); uid = uuid.uuid4()
    buy = _rt(OrderSide.BUY, "2.72", 2, _T0, uid)
    trim = _rt(OrderSide.SELL, "2.85", 1, _T0 + timedelta(minutes=3), uid)
    close = _rt(OrderSide.SELL, "2.90", 1, _T0 + timedelta(minutes=4), uid)
    db.add_all([buy, trim, close]); db.flush()

    assert da._round_trip_summary(db, buy) == {"kind": "enter"}

    st = da._round_trip_summary(db, trim)
    assert st["kind"] == "trim"
    assert st["realized"] == Decimal("13.00")   # (2.85-2.72)*1*100
    assert round(st["pct"]) == 5
    assert st["remaining"] == Decimal("1")
    assert st["original"] == Decimal("2")

    sc = da._round_trip_summary(db, close)
    assert sc["kind"] == "close"
    assert sc["realized"] == Decimal("18.00")   # (2.90-2.72)*1*100
    assert round(sc["pct"]) == 7
    assert sc["total_realized"] == Decimal("31.00")  # 13 + 18
    assert round(sc["total_pct"]) == 6              # 31 / 544


def test_round_trip_single_full_close():
    """A one-shot round-trip: total equals the single leg."""
    db = _session(); uid = uuid.uuid4()
    buy = _rt(OrderSide.BUY, "2.00", 1, _T0, uid)
    sell = _rt(OrderSide.SELL, "3.00", 1, _T0 + timedelta(minutes=5), uid)
    db.add_all([buy, sell]); db.flush()
    s = da._round_trip_summary(db, sell)
    assert s["kind"] == "close"
    assert s["realized"] == Decimal("100.00")       # (3-2)*1*100
    assert s["total_realized"] == Decimal("100.00")


def test_round_trip_naked_sell_is_enter():
    """A SELL with no long to reduce is an ENTER (short), not a trim/close."""
    db = _session(); uid = uuid.uuid4()
    sell = _rt(OrderSide.SELL, "2.00", 1, _T0, uid)
    db.add(sell); db.flush()
    assert da._round_trip_summary(db, sell) == {"kind": "enter"}


def test_round_trip_new_position_after_flat():
    """After a full close, a new BUY starts a FRESH round-trip (total resets)."""
    db = _session(); uid = uuid.uuid4()
    b1 = _rt(OrderSide.BUY, "2.00", 1, _T0, uid)
    s1 = _rt(OrderSide.SELL, "3.00", 1, _T0 + timedelta(minutes=1), uid)   # closes pos 1
    b2 = _rt(OrderSide.BUY, "4.00", 1, _T0 + timedelta(minutes=2), uid)    # new pos 2
    s2 = _rt(OrderSide.SELL, "4.50", 1, _T0 + timedelta(minutes=3), uid)   # closes pos 2
    db.add_all([b1, s1, b2, s2]); db.flush()
    s = da._round_trip_summary(db, s2)
    assert s["kind"] == "close"
    assert s["realized"] == Decimal("50.00")        # (4.5-4)*1*100 — only pos 2
    assert s["total_realized"] == Decimal("50.00")  # NOT 100+50; reset after flat


# ── entry vs close classification (off the trader's own prior fills) ──────────

def test_is_closing_long_roundtrip():
    """BUY to open, then SELL → the SELL is a CLOSE; the opening BUY is an ENTER."""
    db = _session()
    uid = uuid.uuid4()
    buy = _opt(OrderSide.BUY, "2.44", at=_T0, uid=uid)
    sell = _opt(OrderSide.SELL, "0.87", at=_T0 + timedelta(minutes=8), uid=uid)
    db.add_all([buy, sell]); db.flush()
    assert da._is_closing(db, buy) is False    # nothing held before → entering
    assert da._is_closing(db, sell) is True     # sells down the long → closing


def test_is_closing_short_roundtrip():
    """SELL to open (short), then BUY to cover → the BUY is a CLOSE."""
    db = _session()
    uid = uuid.uuid4()
    sell = _opt(OrderSide.SELL, "2.00", at=_T0, uid=uid)
    buy = _opt(OrderSide.BUY, "1.00", at=_T0 + timedelta(minutes=5), uid=uid)
    db.add_all([sell, buy]); db.flush()
    assert da._is_closing(db, sell) is False   # opens the short → entering
    assert da._is_closing(db, buy) is True      # covers the short → closing


def test_is_closing_respects_explicit_flag():
    """An explicit is_closing=True (SnapTrade close) is trusted without history."""
    db = _session()
    o = _opt(OrderSide.SELL, "0.87", closing=True)
    db.add(o); db.flush()
    assert da._is_closing(db, o) is True


def test_is_closing_ignores_other_contracts():
    """A prior fill on a DIFFERENT strike must not make this order look like a
    close."""
    db = _session()
    uid = uuid.uuid4()
    other = _opt(OrderSide.BUY, "3.00", at=_T0, uid=uid)
    other.option_strike = Decimal("500")           # different contract
    sell = _opt(OrderSide.SELL, "0.87", at=_T0 + timedelta(minutes=5), uid=uid)
    db.add_all([other, sell]); db.flush()
    assert da._is_closing(db, sell) is False        # no matching long → entering


# ── copy-paused-at-trade-time (the lag-edge suppression) ────────────────────

def _audit_session() -> Session:
    """In-memory DB with just the audit_logs table (for _copy_paused_at).

    Built with raw DDL (TEXT columns) because the model's JSONB column can't be
    rendered by the SQLite compiler. Insert/query still go through the ORM so
    UUID/datetime binding stays consistent between write and read."""
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE audit_logs ("
            "id TEXT PRIMARY KEY, actor_user_id TEXT, action TEXT, "
            "entity_type TEXT, entity_id TEXT, metadata_json TEXT, "
            "ip_address TEXT, created_at TEXT)"
        )
    return Session(eng)


def _toggle(db, trader, action, hh, mm, ss):
    from app.models.audit_log import AuditLog
    db.add(AuditLog(
        id=uuid.uuid4(), actor_user_id=trader, action=action,
        created_at=datetime(2026, 8, 24, hh, mm, ss, tzinfo=timezone.utc),
    ))


def _at(hh, mm, ss):
    return datetime(2026, 8, 24, hh, mm, ss, tzinfo=timezone.utc)


def test_copy_paused_at_reconstructs_state_by_trade_time():
    """Alert suppression keys off the copy state AT THE TRADE TIME, not detection
    time. Prod 2026-08-24: SNDK filled 14:52:04 during a pause, surfaced 14:52:19
    after resume, and wrongly alerted. Pause 14:51:00, resume 14:52:14."""
    db = _audit_session()
    trader = uuid.uuid4()
    _toggle(db, trader, "trader.copy_paused", 14, 51, 0)
    _toggle(db, trader, "trader.copy_resumed", 14, 52, 14)
    db.commit()

    # Copy ON (before any toggle) → not suppressed → ALERTS.
    assert da._copy_paused_at(db, trader, _at(14, 50, 0)) is False
    # Copy OFF: a fill during the pause → suppressed, even if detected later.
    assert da._copy_paused_at(db, trader, _at(14, 52, 4)) is True
    # Copy back ON: a trade whose time is AFTER resume → alerts normally.
    assert da._copy_paused_at(db, trader, _at(14, 52, 20)) is False
    # Unknown trade time → don't over-suppress.
    assert da._copy_paused_at(db, trader, None) is False


def test_copy_paused_at_no_toggles_means_on():
    """A trader who never paused → copy on by default → never suppressed."""
    db = _audit_session()
    assert da._copy_paused_at(db, uuid.uuid4(), _at(15, 0, 0)) is False
