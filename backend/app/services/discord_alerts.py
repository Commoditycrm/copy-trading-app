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
* Phase 1 scope: entry/close cards WITHOUT realized P&L / hold-time. The CLOSING
  card shows the exit fill only ("Sold N @ price · Position closed"). The
  P&L / %-return / "held Xm" line is Phase 2 (needs FIFO round-trip matching).

Only FILLED orders are alerted, and only the trader's OWN orders
(``parent_order_id IS NULL``) — never a subscriber mirror. Callers fire this
from the same "a trader order just filled" signal that drives
``force_fill_mirrors_to_market``.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.models.audit_log import AuditLog
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus
from app.models.settings import TraderSettings
from app.services import audit

log = logging.getLogger(__name__)

_TIMEOUT = 8.0
# Discord embed color bar: green = entering (buy/open), red = closing (exit).
_COLOR_ENTER = 0x22C55E
_COLOR_CLOSE = 0xEF4444
# Audit action used as the once-per-order dedup marker.
_ALERT_SENT_ACTION = "trader.discord_alert_sent"


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


def build_card(order: Order, is_closing: bool) -> dict:
    """Build the Discord webhook payload (one embed) for a filled trader order.

    Pure — takes a fully-populated Order, returns the JSON body to POST. Phase 1:
    no P&L / hold-time on the CLOSING card."""
    contract = _contract_label(order)
    qty = order.filled_quantity or order.quantity or Decimal(0)
    qty_str = f"{qty.normalize():f}" if isinstance(qty, Decimal) else str(qty)
    price = _fmt_money(order.filled_avg_price)
    now = datetime.now(timezone.utc)

    if is_closing:
        title = f"🔴 CLOSING · {contract}"
        # "Sold 1 @ $0.87" / "Bought 1 @ $0.87" (a close can be either side).
        verb = "Sold" if order.side == OrderSide.SELL else "Bought"
        desc = f"{verb} {qty_str} @ {price}\nPosition closed"
        color = _COLOR_CLOSE
    else:
        title = f"🟢 ENTERING · {contract}"
        notional = _notional(order)
        desc = f"{qty_str} @ {price} · {_fmt_money(notional)}"
        color = _COLOR_ENTER

    return {
        "embeds": [{
            "title": title,
            "description": desc,
            "color": color,
            "footer": {"text": "Kopyya · Not financial advice"},
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
    """Load, gate, dedup, format, send — all on the worker thread."""
    with SessionLocal() as db:
        order = db.get(Order, trader_order_id)
        if order is None or order.parent_order_id is not None:
            return  # gone, or a subscriber mirror — never alert mirrors
        if order.status != OrderStatus.FILLED:
            return
        ts = db.get(TraderSettings, order.user_id)
        if ts is None or not ts.discord_alerts_enabled or not ts.discord_webhook_url:
            return
        # Dedup: skip if we already alerted this order (poll+stream / re-process).
        already = db.execute(
            select(AuditLog.id).where(
                AuditLog.action == _ALERT_SENT_ACTION,
                AuditLog.entity_id == str(order.id),
            ).limit(1)
        ).first()
        if already is not None:
            return
        # Entry vs close — trust an explicit is_closing (SnapTrade) else
        # reconstruct from the trader's own prior fills (needed for Alpaca, which
        # always stores is_closing=False).
        is_closing = _is_closing(db, order)
        payload = build_card(order, is_closing)
        ok = send_webhook(ts.discord_webhook_url, payload)
        # Record the marker even on a failed send so a broken webhook can't cause
        # a retry storm across re-detections; a one-off miss is acceptable for a
        # best-effort feed.
        audit.record(
            db, actor_user_id=order.user_id, action=_ALERT_SENT_ACTION,
            entity_type="order", entity_id=order.id,
            metadata={"sent": ok, "closing": is_closing, "symbol": order.symbol},
        )
        db.commit()


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
            "footer": {"text": "Kopyya · Not financial advice"},
        }]
    }
    return send_webhook(webhook_url, payload)


__all__ = ["build_card", "send_webhook", "emit_trader_fill_alert", "test_webhook"]
