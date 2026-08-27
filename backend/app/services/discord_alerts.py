"""Discord trade-alert broadcast (Phase 1).

Posts an ENTERING / CLOSING card to a trader's Discord "Incoming Webhook" every
time one of THEIR orders fills, so their subscribers see the trade in real time
— the Kopyya-native equivalent of the Alertsify feed.

Design
------
* No new dependency — POSTs a Discord embed over ``httpx`` (already required),
  same shape as ``services/sms.py`` / ``email.py``.
* Best-effort and OFF the trading path: ``emit_trader_fill_alert`` returns
  immediately and does the DB read + HTTP on a daemon thread, so a slow or
  broken webhook can NEVER delay or fail a fill.
* Idempotent: dedup'd by an audit marker so the poll+stream double-detection (or
  a re-processed event) can't post the same card twice.
* Phase 2 cards: ENTERING (green), TRIMMING (amber — a partial close, with
  realized P&L, % and "X of Y still open"), and CLOSING (red — full exit, with
  this leg's P&L + the round-trip TOTAL P&L/%). P&L comes from a long-side FIFO
  reconstruction of the trader's own fills (``_round_trip_summary``); if it can't
  be computed the card safely degrades to the basic enter/close form. Hold-time
  is intentionally omitted.

Only FILLED orders are alerted, and only the trader's OWN orders
(``parent_order_id IS NULL``) — never a subscriber mirror. Callers fire this
from the same "a trader order just filled" signal that drives
``force_fill_mirrors_to_market``.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import func, select, text

from app.database import SessionLocal
from app.models.audit_log import AuditLog
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus
from app.models.settings import TraderSettings
from app.services import audit

log = logging.getLogger(__name__)

_TIMEOUT = 8.0
# Discord embed color bar: green = entering (open), amber = trimming (partial
# close), red = closing (full exit).
_COLOR_ENTER = 0x22C55E
_COLOR_TRIM = 0xF59E0B
_COLOR_CLOSE = 0xEF4444
# Audit action used as the once-per-order dedup marker.
_ALERT_SENT_ACTION = "trader.discord_alert_sent"
_FOOTER = "Broker-verified · Not financial advice · Kopyya"


def _alert_lock_key(order_id: uuid.UUID) -> int:
    """Stable signed 64-bit key for ``pg_advisory_xact_lock``, derived from the
    order id. Every detection path that races to alert the SAME order (listener
    + copy_engine fanout + fills_sync poll — possibly in different processes)
    hashes to one key and serializes on the lock, so only the first writes the
    dedup marker. blake2b (not Python's salted ``hash()``) so the value is
    identical across processes."""
    digest = hashlib.blake2b(f"discord_alert:{order_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _fmt_money(v: Decimal | float | None) -> str:
    """`$1.12`, `$465`, `$1,240` — 2dp for cents, no trailing `.00` on whole $."""
    if v is None:
        return "—"
    d = Decimal(str(v))
    if d == d.to_integral_value():
        return f"${int(d):,}"
    return f"${d:,.2f}"


def _contract_label(o: Order) -> str:
    """`RDGT` for a stock; `AMD $465 PUT · 08/10` for an option — matches the
    Alertsify card title after the ENTERING/CLOSING prefix."""
    sym = (o.symbol or "").upper()
    if o.instrument_type != InstrumentType.OPTION:
        return sym
    parts = [sym]
    if o.option_strike is not None:
        parts.append(_fmt_money(o.option_strike))
    right = getattr(o.option_right, "value", o.option_right)
    if right:
        parts.append(str(right).upper())
    label = " ".join(parts)
    if o.option_expiry is not None:
        # MM/DD to match the feed ("08/10").
        label = f"{label} · {o.option_expiry:%m/%d}"
    return label


def _notional(o: Order) -> Decimal | None:
    """Filled dollar size: price × qty, ×100 per contract for options."""
    px = o.filled_avg_price
    qty = o.filled_quantity or o.quantity
    if px is None or qty is None:
        return None
    mult = Decimal(100) if o.instrument_type == InstrumentType.OPTION else Decimal(1)
    return Decimal(str(px)) * Decimal(str(qty)) * mult


def _qty_str(q) -> str:
    """`1`, `2`, `1.5` — no trailing zeros, integers show plain."""
    d = Decimal(str(q))
    return str(int(d)) if d == d.to_integral_value() else f"{d.normalize():f}"


def _fmt_signed(v: Decimal | float) -> str:
    """`+$13`, `-$5`, `+$1,240` — signed dollar with thousands, whole = no cents."""
    d = Decimal(str(v))
    sign = "+" if d >= 0 else "-"
    a = abs(d)
    body = f"{int(a):,}" if a == a.to_integral_value() else f"{a:,.2f}"
    return f"{sign}${body}"


def _fmt_pct(p: float | None) -> str:
    """`+5%`, `-3%` — rounded to whole percent, signed. '' if unknown."""
    if p is None:
        return ""
    return f"{'+' if p >= 0 else ''}{p:.0f}%"


def _round_trip_summary(db, order: Order) -> dict:
    """Classify a filled trader order and compute round-trip P&L via FIFO.

    Reconstructs the trader's position in THIS contract from their own filled
    orders (long-side FIFO — these traders run long options/stocks). Returns a
    dict describing the card to render:

      {'kind': 'enter'}                       — opened / added
      {'kind': 'trim', realized, pct,          — sold PART of a long
                remaining, original}
      {'kind': 'close', realized, pct,         — sold the LAST of a long
                total_realized, total_pct}

    ``realized``/``pct`` are for THIS sell; ``total_*`` are cumulative across the
    whole position (all trims + this close). A sell that doesn't reduce a long
    (naked short / no position), and any buy, is an 'enter'. Never raises — the
    caller falls back to a basic card on error."""
    mult = Decimal(100) if order.instrument_type == InstrumentType.OPTION else Decimal(1)
    rows = db.execute(
        select(Order).where(*_contract_filter(order)).order_by(Order.created_at, Order.id)
    ).scalars().all()

    lots: deque[list] = deque()      # open long lots: [qty, price]
    entered = Decimal(0)             # cumulative entry qty for the CURRENT position
    pos_realized = Decimal(0)        # cumulative realized $ for the current position
    pos_cost = Decimal(0)            # cost basis of the shares sold this position
    result: dict = {"kind": "enter"}

    for o in rows:
        q = o.filled_quantity or o.quantity or Decimal(0)
        px = o.filled_avg_price
        is_target = o.id == order.id
        if q <= 0 or px is None:
            if is_target:
                return {"kind": "enter"}
            continue
        if o.side == OrderSide.BUY:
            if not lots:                         # position was flat → new position
                entered = pos_realized = pos_cost = Decimal(0)
            lots.append([q, px])
            entered += q
            if is_target:
                result = {"kind": "enter"}
        else:                                    # SELL — match against long lots
            rem, rlz, cost = q, Decimal(0), Decimal(0)
            while rem > 0 and lots:
                lot = lots[0]
                take = min(lot[0], rem)
                rlz += (px - lot[1]) * take * mult
                cost += lot[1] * take * mult
                lot[0] -= take
                rem -= take
                if lot[0] <= 0:
                    lots.popleft()
            if cost == 0:                        # didn't reduce a long → treat as enter
                if is_target:
                    result = {"kind": "enter"}
            else:
                pos_realized += rlz
                pos_cost += cost
                remaining = sum((lot[0] for lot in lots), Decimal(0))
                if is_target:
                    if remaining <= 0:
                        result = {
                            "kind": "close",
                            "realized": rlz,
                            "pct": float(rlz / cost * 100) if cost else None,
                            "total_realized": pos_realized,
                            "total_pct": float(pos_realized / pos_cost * 100) if pos_cost else None,
                        }
                    else:
                        result = {
                            "kind": "trim",
                            "realized": rlz,
                            "pct": float(rlz / cost * 100) if cost else None,
                            "remaining": remaining,
                            "original": entered,
                        }
                if remaining <= 0:               # position closed → reset for next round-trip
                    entered = pos_realized = pos_cost = Decimal(0)
        if is_target:
            break
    return result


def build_card(order: Order, summary: dict | None = None) -> dict:
    """Build the Discord webhook payload (one embed) for a filled trader order.

    ``summary`` comes from ``_round_trip_summary`` and drives the card type:
    ENTERING (green) / TRIMMING (amber, partial close w/ P&L + "X of Y open") /
    CLOSING (red, full exit w/ P&L + round-trip total). None → ENTERING."""
    contract = _contract_label(order)
    qty_str = _qty_str(order.filled_quantity or order.quantity or Decimal(0))
    price = _fmt_money(order.filled_avg_price)
    verb = "Sold" if order.side == OrderSide.SELL else "Bought"
    kind = (summary or {}).get("kind", "enter")
    now = datetime.now(timezone.utc)

    if kind == "trim":
        pnl = _fmt_signed(summary["realized"])
        pct = f" · {_fmt_pct(summary.get('pct'))}" if summary.get("pct") is not None else ""
        rem, orig = _qty_str(summary["remaining"]), _qty_str(summary["original"])
        title = f"🟡 TRIMMING · {contract}"
        desc = f"{verb} {qty_str} @ {price} · {pnl}{pct}\n{rem} of {orig} still open"
        color = _COLOR_TRIM
    elif kind == "close":
        pnl = _fmt_signed(summary["realized"])
        pct = f" · {_fmt_pct(summary.get('pct'))}" if summary.get("pct") is not None else ""
        tot = _fmt_signed(summary["total_realized"])
        totpct = f" · {_fmt_pct(summary.get('total_pct'))}" if summary.get("total_pct") is not None else ""
        title = f"🔴 CLOSING · {contract}"
        desc = f"{verb} {qty_str} @ {price} · {pnl}{pct}\nPosition closed · total {tot}{totpct}"
        color = _COLOR_CLOSE
    else:  # enter
        title = f"🟢 ENTERING · {contract}"
        desc = f"{qty_str} @ {price} · {_fmt_money(_notional(order))}"
        color = _COLOR_ENTER

    return {
        "embeds": [{
            "title": title,
            "description": desc,
            "color": color,
            "footer": {"text": _FOOTER},
            "timestamp": now.isoformat(),
        }]
    }


def _contract_filter(order: Order):
    """SQLAlchemy predicates matching the SAME contract as ``order`` — symbol for
    a stock, symbol+strike+right+expiry for an option."""
    preds = [
        Order.user_id == order.user_id,
        Order.symbol == order.symbol,
        Order.instrument_type == order.instrument_type,
        Order.parent_order_id.is_(None),
        Order.status == OrderStatus.FILLED,
    ]
    if order.instrument_type == InstrumentType.OPTION:
        preds += [
            Order.option_strike == order.option_strike,
            Order.option_right == order.option_right,
            Order.option_expiry == order.option_expiry,
        ]
    return preds


def _is_closing(db, order: Order) -> bool:
    """Decide ENTERING vs CLOSING for a filled trader order.

    The persisted ``is_closing`` flag is only reliable on brokers that report an
    explicit open/close action (SnapTrade). The Alpaca listener always stores
    False (it classifies per-subscriber later from held qty), so we can't trust
    it for an Alpaca trader. Instead reconstruct the trader's NET position in
    this contract from their OWN filled orders BEFORE this one: a SELL that draws
    down a long — or a BUY that covers a short — is a CLOSE; anything that grows
    the position (or opens a fresh one) is an ENTER.

    Prefer an explicit ``is_closing=True`` when set (SnapTrade close); otherwise
    fall back to the position reconstruction."""
    if bool(order.is_closing):
        return True
    prior = db.execute(
        select(Order.side, Order.filled_quantity, Order.quantity).where(
            *_contract_filter(order),
            Order.id != order.id,
            Order.created_at < order.created_at,
        )
    ).all()
    net = Decimal(0)  # +long / -short held before this order
    for side, fq, qty in prior:
        q = fq if fq is not None else (qty or Decimal(0))
        net += q if side == OrderSide.BUY else -q
    if order.side == OrderSide.BUY:
        return net < 0    # buying back a short → closing
    return net > 0        # selling down a long → closing


def send_webhook(webhook_url: str, payload: dict) -> bool:
    """POST a payload to a Discord Incoming Webhook. Returns True on 2xx (Discord
    returns 204). Never raises — alerts are best-effort. Honors one 429 retry."""
    for attempt in range(2):
        try:
            resp = httpx.post(webhook_url, json=payload, timeout=_TIMEOUT)
        except Exception:  # noqa: BLE001
            log.warning("discord: webhook POST failed", exc_info=True)
            return False
        if resp.status_code // 100 == 2:
            return True
        if resp.status_code == 429 and attempt == 0:
            # Rate limited — Discord tells us how long to wait.
            try:
                retry_after = float(resp.json().get("retry_after", 1.0))
            except Exception:  # noqa: BLE001
                retry_after = 1.0
            import time  # noqa: PLC0415
            time.sleep(min(retry_after, 5.0))
            continue
        log.warning("discord: webhook rejected status=%s body=%s", resp.status_code, resp.text[:300])
        return False
    return False


def _run(trader_order_id: uuid.UUID) -> None:
    """Load, gate, claim, format, send — all on the worker thread."""
    with SessionLocal() as db:
        order = db.get(Order, trader_order_id)
        if order is None or order.parent_order_id is not None:
            return  # gone, or a subscriber mirror — never alert mirrors
        if order.status != OrderStatus.FILLED:
            return
        ts = db.get(TraderSettings, order.user_id)
        if ts is None or not ts.discord_alerts_enabled or not ts.discord_webhook_url:
            return
        webhook_url = ts.discord_webhook_url

        # Atomically CLAIM this order's alert. Several paths detect the same
        # fill almost simultaneously (SnapTrade/Webull listener + copy_engine
        # fanout + fills_sync poll), each on its own daemon thread — and the
        # bare "SELECT marker? then send then write marker" is a check-then-act
        # race: two threads both see no marker, both send → duplicate card
        # (the bug we saw on the Friday TSLA order). Serialize on a per-order
        # advisory lock, re-check the marker inside it, then write the marker
        # and COMMIT *before* the HTTP send. The commit releases the lock, so
        # the loser wakes, sees the marker, and bails — and we never hold a DB
        # connection across the webhook call.
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _alert_lock_key(order.id)})
        already = db.execute(
            select(AuditLog.id).where(
                AuditLog.action == _ALERT_SENT_ACTION,
                AuditLog.entity_id == str(order.id),
            ).limit(1)
        ).first()
        if already is not None:
            return  # another path already claimed/sent this order's card
        # Simple rule: Discord alerts ON → every filled trader order gets a card,
        # regardless of copy on/off. (OFF is handled by the enabled check above.)
        # Classify + compute round-trip P&L (ENTERING / TRIMMING / CLOSING).
        # If the FIFO calc fails for any reason, fall back to the basic
        # enter/close card (Phase-1 behaviour) so a bad calc can never break the
        # feed — the card is always at least as good as before.
        try:
            summary = _round_trip_summary(db, order)
        except Exception:  # noqa: BLE001
            log.warning("discord: round-trip calc failed for %s; basic card", order.id, exc_info=True)
            summary = {"kind": "close" if _is_closing(db, order) else "enter"}
        payload = build_card(order, summary)
        # Write the dedup marker as the CLAIM (before sending) and commit to
        # release the advisory lock. A broken/slow webhook can then never cause
        # a duplicate; the trade-off is that a crash in the tiny window between
        # this commit and the send below drops one card — acceptable for a
        # best-effort feed (the same window a failed send already couldn't retry).
        audit.record(
            db, actor_user_id=order.user_id, action=_ALERT_SENT_ACTION,
            entity_type="order", entity_id=order.id,
            metadata={"kind": summary.get("kind"), "symbol": order.symbol},
        )
        db.commit()

    # Send OUTSIDE the lock/transaction — best-effort.
    if not send_webhook(webhook_url, payload):
        log.warning(
            "discord: webhook send failed for order=%s (marker already recorded, won't retry)",
            trader_order_id,
        )


def emit_pending_trader_alerts(db, user_id: uuid.UUID, window_minutes: int = 30) -> None:
    """Sweep: emit a card for EVERY of this trader's FILLED orders in the recent
    window that hasn't been alerted yet. Path-independent — it looks at the
    CURRENT state (filled + no marker), so it catches orders completed by ANY
    fill path (socket, fanout, fills_sync) and self-heals ones a prior run
    missed. Idempotent via the per-order marker. Cheap: one indexed SELECT for a
    single trader. No-op unless the trader has Discord alerts configured.

    ``db`` should reflect COMMITTED order state (call after commit) so each
    spawned emit thread — which opens its own session — sees the FILLED rows.

    The window is on the TRADE time (closed_at/submitted_at), NOT created_at.
    created_at is the ROW-INSERT time (server_default now), and fills_sync
    SYNTHESIZES rows for historical/external trades with created_at=now but the
    real (old) trade time in submitted_at/closed_at. Keying off created_at made
    a backfill of old trades look "just filled" and posted the whole backlog to
    Discord (prod incident after a deploy: a flood of old CLOSING cards). Trade
    time excludes those while still catching genuinely-recent missed fills."""
    ts = db.get(TraderSettings, user_id)
    if ts is None or not ts.discord_alerts_enabled or not ts.discord_webhook_url:
        return
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    trade_time = func.coalesce(Order.closed_at, Order.submitted_at, Order.created_at)
    oids = db.execute(
        select(Order.id).where(
            Order.user_id == user_id,
            Order.parent_order_id.is_(None),
            Order.status == OrderStatus.FILLED,
            trade_time > since,
        )
    ).scalars().all()
    if not oids:
        return
    marked = set(db.execute(
        select(AuditLog.entity_id).where(
            AuditLog.action == _ALERT_SENT_ACTION,
            AuditLog.entity_id.in_([str(o) for o in oids]),
        )
    ).scalars().all())
    for oid in oids:
        if str(oid) not in marked:
            emit_trader_fill_alert(oid)


def suppress_pending_trader_alerts(db, user_id: uuid.UUID, window_hours: int = 6) -> int:
    """Claim the backlog of a trader's recently-FILLED orders — write the
    once-per-order sent-marker WITHOUT sending — so trades taken while Discord
    alerts were OFF are not retroactively pushed when alerts are turned back ON.

    The problem: markers are only written when an alert is sent, so trades made
    with alerts OFF accumulate unmarked; the moment alerts flip ON, the
    ``emit_pending_trader_alerts`` sweep finds that whole unmarked backlog (their
    trade time is recent) and blasts every card at once (prod: a trader who
    toggled copy+alerts off, traded, then back on, saw all the off-period trades
    fire). Called on the OFF→ON transition BEFORE the caller commits, so any
    later sweep sees the markers and skips; a concurrent sweep is also safe
    because ``_run`` re-checks the marker inside its per-order advisory lock.

    Bounded to the recent window the sweep can actually reach (its 30-min
    trade-time window, plus margin) so only sweep-eligible fills are claimed —
    older off-period trades fall outside the sweep window anyway. Returns the
    number of markers written. Only NEW fills (after this point) will alert."""
    ts = db.get(TraderSettings, user_id)
    if ts is None or not ts.discord_alerts_enabled:
        return 0
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    trade_time = func.coalesce(Order.closed_at, Order.submitted_at, Order.created_at)
    oids = db.execute(
        select(Order.id).where(
            Order.user_id == user_id,
            Order.parent_order_id.is_(None),
            Order.status == OrderStatus.FILLED,
            trade_time > since,
        )
    ).scalars().all()
    if not oids:
        return 0
    marked = set(db.execute(
        select(AuditLog.entity_id).where(
            AuditLog.action == _ALERT_SENT_ACTION,
            AuditLog.entity_id.in_([str(o) for o in oids]),
        )
    ).scalars().all())
    written = 0
    for oid in oids:
        if str(oid) not in marked:
            audit.record(
                db, actor_user_id=user_id, action=_ALERT_SENT_ACTION,
                entity_type="order", entity_id=oid,
                metadata={"suppressed_on_enable": True},
            )
            written += 1
    return written


def emit_trader_fill_alert(trader_order_id: uuid.UUID) -> None:
    """Fire-and-forget a Discord card for a trader's just-FILLED order.

    Returns immediately — the DB read + HTTP happen on a daemon thread so the
    listener/fill path is never blocked. Safe to call unconditionally; all
    gating (enabled flag, webhook set, is-a-trader-order, dedup) is inside
    ``_run``."""
    try:
        threading.Thread(target=_run, args=(trader_order_id,), daemon=True).start()
    except Exception:  # noqa: BLE001
        log.warning("discord: failed to spawn alert thread", exc_info=True)


def test_webhook(webhook_url: str) -> bool:
    """Send a one-off 'connected' card so the trader can verify their webhook
    from the Settings page before enabling live alerts."""
    payload = {
        "embeds": [{
            "title": "✅ Kopyya alerts connected",
            "description": "Your trade alerts will post here.",
            "color": _COLOR_ENTER,
            "footer": {"text": _FOOTER},
        }]
    }
    return send_webhook(webhook_url, payload)


__all__ = ["build_card", "send_webhook", "emit_trader_fill_alert", "test_webhook"]
