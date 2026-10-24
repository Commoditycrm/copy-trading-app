"""Sync filled trades from Alpaca into our local fills + orders tables.

For each FILL activity Alpaca reports, we:
  1. Dedup by activity.id (mapped to Fill.broker_fill_id).
  2. Create a synthetic Order row (status=FILLED) — activities don't always
     carry a usable order_id linkage, so we treat each activity as a
     self-contained order. This means fills from external trades (placed
     in Alpaca's UI directly) also surface in our Trades / Calendar.
  3. Insert a Fill row attached to that Order.

Caller commits the session.
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.brokers import adapter_for
from app.brokers.alpaca import AlpacaAdapter, _looks_like_occ, _parse_occ
from app.database import release_connection
from app.models.broker_account import BrokerAccount
from app.models.order import Fill, InstrumentType, Order, OrderSide, OrderStatus, OrderType
from app.services import order_intent
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

# Grace window before fills_sync will SYNTHESIZE an order for a fill it can't
# match to an existing row. A just-happened fill with no matching order is
# almost always an order placed through our app whose row hasn't committed its
# broker_order_id yet (the place endpoint commits only after the broker
# round-trip). Synthesizing here would duplicate that order — and then fan the
# duplicate out to subscribers. So we skip unmatched fills younger than this;
# the next sync either matches by broker_order_id (app row now committed) or, if
# the fill is genuinely external (placed in the broker's own UI), synthesizes it
# once it ages past the window. The only cost is a short delay before truly
# external fills appear.
_SYNTH_ORDER_GRACE = timedelta(seconds=90)


@dataclass
class SyncResult:
    fills_added: int
    orders_added: int
    activities_seen: int
    skipped: int


def _dec(v: Any) -> Decimal:
    if v is None:
        return Decimal(0)
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return Decimal(0)


def _as_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """SDK responses are pydantic-ish objects but sometimes dicts; tolerant lookup."""
    for n in names:
        if isinstance(obj, dict):
            v = obj.get(n)
        else:
            v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


_NON_TERMINAL_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)


# How far back repair_unrecorded_fills looks. A fill older than this is left
# alone: the repair is for the gap a confirmation left today, not an archive scan.
_REPAIR_WINDOW = timedelta(hours=24)
# At most this many broker reads per call, so a backlog cannot eat a
# rate-limited broker's budget (direct Webull: ~10 requests / 30s).
_REPAIR_MAX_READS = 3
# An order the broker still cannot report as filled after this long is stamped
# closed and left alone.
_REPAIR_GIVE_UP = timedelta(hours=1)


def repair_unrecorded_fills(db: Session, acct: BrokerAccount, adapter: Any) -> int:
    """Fill in the price and time of orders we marked FILLED without them.

    A sell confirmed by exit_confirm used to be recorded FILLED with no price and
    no close time, and an order the sweep above never re-reads (it is terminal)
    stayed that way: Closed today, which picks a day's sells by fill/close time,
    and realized P&L both left it out. Each such order is read from the broker
    once — the close time is stamped whether or not a price comes back, so it is
    never read again.
    """
    from app.services.exit_confirm import record_fill_details  # noqa: PLC0415

    rows = list(db.execute(
        select(Order).where(
            Order.broker_account_id == acct.id,
            Order.status == OrderStatus.FILLED,
            Order.closed_at.is_(None),
            Order.broker_order_id.isnot(None),
            Order.created_at >= datetime.now(timezone.utc) - _REPAIR_WINDOW,
        ).order_by(Order.created_at).limit(_REPAIR_MAX_READS)
    ).scalars())
    repaired = 0
    now = datetime.now(timezone.utc)
    for order in rows:
        try:
            res = adapter.get_order(order.broker_order_id)
        except Exception:  # noqa: BLE001
            log.warning("fills_sync: repair read of %s failed", order.id, exc_info=True)
            res = None
        if getattr(res, "status", None) != OrderStatus.FILLED:
            # Unreadable, or the broker no longer says filled. Retried on later
            # ticks for a while (a read can fail on a rate limit), then given up
            # so one order the broker cannot find does not spend every tick.
            created = order.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created is not None and now - created > _REPAIR_GIVE_UP:
                order.closed_at = now
                repaired += 1
            continue
        record_fill_details(order, res)
        repaired += 1
        log.info("fills_sync: recorded the fill of %s %s (price %s)",
                 order.symbol, order.id, order.filled_avg_price)
    repaired += _repair_cancelled_sells(db, acct, adapter, _REPAIR_MAX_READS - len(rows), now)
    if repaired:
        db.flush()
    return repaired


def _repair_cancelled_sells(db: Session, acct: BrokerAccount, adapter: Any,
                            budget: int, now: datetime) -> int:
    """Ask the broker once about each limit sell we recorded CANCELED with
    nothing traded and no close time — the mark of a cancel we recorded
    ourselves (an end the broker reports stamps a close time).

    A take-profit can fill in the moment it is cancelled, and a cancel the
    broker could not confirm was recorded as a cancel: QA 2026-10-19, QQQ 722P —
    Trim 1 sold 8 @ 1.22 at Webull, and the sale was missing from the timeline,
    Closed today and the day's P&L. A fill found is recorded as a fill; anything
    else is stamped closed, so each order is read once.
    """
    from app.services.exit_confirm import _filled_quantity, record_fill_details  # noqa: PLC0415

    if budget <= 0:
        return 0
    rows = list(db.execute(
        select(Order).where(
            Order.broker_account_id == acct.id,
            Order.status == OrderStatus.CANCELED,
            Order.side == OrderSide.SELL,
            Order.order_type == OrderType.LIMIT,
            Order.closed_at.is_(None),
            Order.broker_order_id.isnot(None),
            func.coalesce(Order.filled_quantity, 0) == 0,
            Order.created_at >= now - _REPAIR_WINDOW,
        ).order_by(Order.created_at.desc()).limit(budget)
    ).scalars())
    repaired = 0
    for order in rows:
        try:
            res = adapter.get_order(order.broker_order_id)
        except Exception:  # noqa: BLE001
            log.warning("fills_sync: re-read of cancelled %s failed", order.id, exc_info=True)
            res = None
        status = getattr(res, "status", None)
        if status is None:
            created = order.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created is not None and now - created > _REPAIR_GIVE_UP:
                order.closed_at = now
                repaired += 1
            continue
        got = _filled_quantity(res)
        if status == OrderStatus.FILLED or got > 0:
            if got > 0:
                order.filled_quantity = got
            if status == OrderStatus.FILLED:
                order.status = OrderStatus.FILLED
            record_fill_details(order, res)
            log.warning("fills_sync: %s %s was recorded cancelled but %s at the broker "
                        "(%s @ %s) — recorded the fill",
                        order.symbol, order.id, getattr(status, "value", status),
                        order.filled_quantity, order.filled_avg_price)
        else:
            order.closed_at = now
        repaired += 1
    return repaired


def _refresh_open_orders(
    db: Session, acct: BrokerAccount, adapter: Any, changed_out: "list[Order] | None" = None,
) -> int:
    """Poll the broker's order endpoint for every non-terminal order on this
    account. Alpaca's order resource updates instantly on fill, while the
    activities feed can lag minutes. This keeps Order History fresh without
    waiting for activities."""
    open_orders = list(db.execute(
        select(Order).where(
            Order.broker_account_id == acct.id,
            Order.status.in_(_NON_TERMINAL_STATUSES),
            Order.broker_order_id.isnot(None),
        )
    ).scalars())

    # Batch fast path. Adapters that can return every recent order in ONE call
    # expose get_orders_snapshot; we prefer it over per-order reads whenever
    # there's more than one order to check. This matters on brokers with tight
    # per-key rate limits (direct Webull: ~10 requests / 30s, and this sweep runs
    # every 30s) — without it, an account with several working orders spends its
    # whole budget here and the throttled reads fail silently in the loop below,
    # leaving fills unsynced with nothing in the logs to say so.
    #
    # Strictly an optimisation: anything the snapshot doesn't cover (an order
    # from a previous day, a page overflow, a failed call) still goes through
    # get_order exactly as before.
    snapshot: dict[str, Any] | None = None
    if len(open_orders) > 1:
        _batch = getattr(adapter, "get_orders_snapshot", None)
        if _batch is not None:
            try:
                snapshot = _batch() or None
            except Exception:  # noqa: BLE001
                log.warning(
                    "fills_sync: batch order snapshot failed for %s — falling back "
                    "to per-order reads", acct.id, exc_info=True,
                )
                snapshot = None

    refreshed = 0
    # Bracket exit legs that flip to FILLED here closed a position. Subscribers
    # have no real-time fill listener, so this poll-driven sync is the only
    # place we learn a copied TP/SL hit — surface it as an SSE so the positions
    # table can refresh live instead of only on manual reload.
    newly_closed: list[Order] = []
    # Mirror ENTRIES that just transitioned — used to fire/cancel any close we
    # deferred behind them (a close that arrived before its entry filled).
    filled_entries: list[Order] = []
    dead_entries: list[Order] = []
    _TERMINAL = (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED)
    for order in open_orders:
        res = snapshot.get(order.broker_order_id) if snapshot else None
        if res is None:
            try:
                res = adapter.get_order(order.broker_order_id)
            except Exception:  # noqa: BLE001
                # Order may have been cancelled at the broker side or the id is
                # stale — don't fail the whole sync over one bad lookup.
                continue
        prev_status = order.status
        changed = False
        if res.status != order.status:
            order.status = res.status
            changed = True
        if res.filled_quantity is not None and _dec(res.filled_quantity) != _dec(order.filled_quantity):
            order.filled_quantity = res.filled_quantity
            changed = True
        if res.filled_avg_price is not None and _dec(res.filled_avg_price) != _dec(order.filled_avg_price):
            order.filled_avg_price = res.filled_avg_price
            changed = True
        # FILLED with a zero fill quantity is self-contradictory — FILLED means
        # the whole order traded. It means we could not PARSE the quantity out of
        # the broker's response, and writing it anyway is unrecoverable: this
        # sweep only re-reads NON-terminal orders, so the row is terminal, wrong,
        # and never looked at again.
        #
        # It is not a theoretical inconsistency. Webull spells the field
        # filled_quantity on its order-detail endpoint and filled_qty on Query
        # Day Orders; only the second was handled, so every filled mirror landed
        # FILLED with 0. copy_engine._closeable_quantity sums that column, so the
        # subscriber read as FLAT while holding the position and their next close
        # went out as an OPENING sell, which the broker rejected.
        #
        # So trust the status and take the quantity from the order itself — true
        # by definition of FILLED — and log loudly, because reaching here at all
        # means a parser needs fixing.
        if (
            res.status == OrderStatus.FILLED
            and _dec(order.filled_quantity) == 0
            and _dec(order.quantity) > 0
        ):
            log.warning(
                "fills_sync: %s reports order %s FILLED but no fill quantity "
                "parsed — assuming the full %s. THIS INDICATES A PARSER GAP in "
                "the adapter's order-detail mapping; fix it rather than relying "
                "on this fallback.",
                acct.broker.value, order.broker_order_id, order.quantity,
            )
            order.filled_quantity = order.quantity
            changed = True
        # The BROKER's own execution time, when it reports one. Kept separate
        # from closed_at (set below to OUR clock) so the gap between them is a
        # measurable detection lag rather than an invisible conflation — that
        # conflation is why Order History's "Time Taken to Filled" was showing
        # our polling latency on every broker except Alpaca.
        if res.filled_at is not None and order.broker_filled_at is None:
            order.broker_filled_at = res.filled_at
            changed = True
        if res.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED) and order.closed_at is None:
            order.closed_at = datetime.now(timezone.utc)
            changed = True
        if (
            res.status == OrderStatus.FILLED
            and prev_status != OrderStatus.FILLED
            and order.bracket_leg is not None
        ):
            newly_closed.append(order)
        # Track a mirror ENTRY transition for the deferred-close hook below.
        if changed and order.parent_order_id is not None and not order.is_closing:
            if res.status == OrderStatus.FILLED and prev_status != OrderStatus.FILLED:
                filled_entries.append(order)
            elif res.status in _TERMINAL and prev_status not in _TERMINAL:
                dead_entries.append(order)
        if changed:
            refreshed += 1
            if changed_out is not None:
                changed_out.append(order)

    for order in newly_closed:
        try:
            from app.services import events as _events  # noqa: PLC0415
            _events.publish(order.user_id, {
                "type": "position.auto_closed",
                "leg": order.bracket_leg,
                "symbol": order.symbol,
                "qty": str(order.filled_quantity) if order.filled_quantity is not None else None,
                "broker": acct.broker.value,
            })
        except Exception:  # noqa: BLE001
            log.exception(
                "fills_sync: position.auto_closed publish failed for order=%s", order.id
            )

    # Deferred-close hook: a subscriber entry that just FILLED fires any close
    # parked behind it; one that DIED cancels it. Run after the caller commits,
    # off this thread — see _queue_entry_hooks.
    if filled_entries or dead_entries:
        _queue_entry_hooks(db, filled_entries, dead_entries)
    return refreshed


# The entry hooks open their own sessions. Run inline they made the caller —
# holding a pool connection and, via sync_user_fills, the per-user advisory
# lock — wait on a SECOND connection. QA 2026-10-21: requests queued on that
# lock filled the pool, so each hook sat out pool_timeout while still holding
# the lock, and the web tier 500'd until restart. Queued on the session and run
# in a thread once its transaction commits; dropped on rollback, so the next
# sweep sees the transition again.
_ENTRY_HOOKS = "fills_sync.entry_hooks"


def _queue_entry_hooks(db: Session, filled: "list[Order]", dead: "list[Order]") -> None:
    # Column snapshots: the rows expire at commit and must not lazy-load
    # through the caller's session from another thread.
    def snap(o: Order) -> SimpleNamespace:
        return SimpleNamespace(**{c.key: getattr(o, c.key) for c in Order.__mapper__.column_attrs})

    q = db.info.setdefault(_ENTRY_HOOKS, ([], []))
    q[0].extend(snap(e) for e in filled)
    q[1].extend(snap(e) for e in dead)


@event.listens_for(Session, "after_commit")
def _entry_hooks_after_commit(session: Session) -> None:
    if session.in_nested_transaction():
        return
    q = session.info.pop(_ENTRY_HOOKS, None)
    if q:
        threading.Thread(target=_run_entry_hooks, args=q, daemon=True,
                         name="fills-entry-hooks").start()


@event.listens_for(Session, "after_rollback")
def _entry_hooks_after_rollback(session: Session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_ENTRY_HOOKS, None)


def _run_entry_hooks(filled: "list[Any]", dead: "list[Any]") -> None:
    from app.services import copy_engine  # noqa: PLC0415
    from app.services import events as _events  # noqa: PLC0415
    for e in filled:
        # A mirror entry just filled → a new open position exists. The
        # subscriber has no real-time listener, so this poll is the only
        # place that fill is seen; publish order.placed (the same event the
        # trader websocket emits on fill, and the one the positions table
        # already refreshes on) so the position shows up live instead of
        # only after a manual reload.
        try:
            _events.publish(e.user_id, copy_engine._order_event("order.placed", e))  # noqa: SLF001
        except Exception:  # noqa: BLE001
            log.exception("fills_sync: order.placed publish failed for entry=%s", e.id)
        try:
            copy_engine.fire_deferred_closes_for_entry(e)
        except Exception:  # noqa: BLE001
            log.exception("fills_sync: fire deferred closes failed for entry=%s", e.id)
    for e in dead:
        try:
            copy_engine.cancel_deferred_closes_for_entry(e)
        except Exception:  # noqa: BLE001
            log.exception("fills_sync: cancel deferred closes failed for entry=%s", e.id)


def sync_account_fills(db: Session, acct: BrokerAccount, adapter: Any = None) -> SyncResult:
    """Pull activities for `acct` from its broker and upsert fills locally.
    Idempotent — re-runs are safe. ``adapter`` may be a _Prefetched one, so
    the writes here run without broker I/O (see sync_user_fills)."""
    if adapter is None:
        adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
        if not isinstance(adapter, AlpacaAdapter):
            return SyncResult(fills_added=0, orders_added=0, activities_seen=0, skipped=0)

    # Fast path: refresh status/qty/price for orders we placed that haven't
    # terminalized yet. Catches fills the moment they happen, instead of
    # waiting for the activities feed.
    _refresh_open_orders(db, acct, adapter)

    activities = adapter.list_recent_activities()

    fills_added = 0
    orders_added = 0
    skipped = 0

    # Pre-fetch existing fill ids for this account so we don't round-trip
    # the DB once per activity.
    existing: set[str] = set(
        r[0] for r in db.execute(
            select(Fill.broker_fill_id)
            .join(Order, Fill.order_id == Order.id)
            .where(Order.broker_account_id == acct.id, Fill.broker_fill_id.isnot(None))
        ).all()
    )

    for entry in activities:
        # Alpaca's account-activities endpoint returns mixed types.
        # We only care about FILL events. activity_type is e.g. "FILL", "DIV", "FEE".
        atype = str(_attr(entry, "activity_type")).upper()
        if atype != "FILL":
            skipped += 1
            continue

        # FILL fields: id, transaction_time, type (fill/partial_fill), price,
        # qty, side (buy/sell), symbol, order_id, cum_qty, leaves_qty, ...
        activity_id = str(_attr(entry, "id") or "")
        if not activity_id or activity_id in existing:
            skipped += 1
            continue

        side_raw = str(_attr(entry, "side") or "").lower()
        if side_raw not in ("buy", "sell"):
            skipped += 1
            continue
        side = OrderSide.BUY if side_raw == "buy" else OrderSide.SELL

        symbol_full = str(_attr(entry, "symbol") or "")
        if not symbol_full:
            skipped += 1
            continue

        # Alpaca's activity feed returns the UNPADDED OCC symbol (root + YYMMDD +
        # C/P + 8-digit strike), so total length tracks the ticker: a 1–2 char
        # root is only 16–17 chars. Use the shared OCC parser, not a length gate
        # — a >=18 gate mis-tags short-ticker options (T, VG, MU…) as STOCK and
        # they lose the 100x contract multiplier in realized P&L.
        parsed = _parse_occ(symbol_full) if _looks_like_occ(symbol_full) else None
        if parsed is not None:
            display_symbol, option_expiry, option_strike, option_right = parsed
            instrument = InstrumentType.OPTION
        else:
            instrument = InstrumentType.STOCK
            display_symbol = symbol_full.upper()
            option_expiry = None
            option_strike = None
            option_right = None

        units = _dec(_attr(entry, "qty"))
        price = _dec(_attr(entry, "price"))
        if units <= 0 or price <= 0:
            skipped += 1
            continue

        trade_at = _as_dt(_attr(entry, "transaction_time")) or datetime.now(timezone.utc)

        # If the activity references a broker order_id that matches an Order
        # we already have, update THAT order in place. Otherwise (external
        # trades placed in Alpaca's UI directly, or pre-existing fills) create
        # a synthetic order so the activity still surfaces in the UI.
        #
        # Match by (user_id, broker_order_id) — NOT broker_account_id. When a
        # broker is deleted and reconnected, the old rows survive with
        # broker_account_id=NULL (ON DELETE SET NULL) under a NEW account id.
        # Scoping the match to the current account would miss them and
        # re-import the whole history as duplicates on every reconnect.
        # Matching by broker_order_id (globally unique per broker) finds the
        # existing row and we re-adopt it onto the reconnected account.
        broker_oid = str(_attr(entry, "order_id") or "") or None
        order: Order | None = None
        if broker_oid:
            order = db.execute(
                select(Order).where(
                    Order.user_id == acct.user_id,
                    Order.broker_order_id == broker_oid,
                ).order_by(Order.created_at.asc()).limit(1)
            ).scalar_one_or_none()
            # Re-adopt an orphaned (or differently-accounted) match onto the
            # account we're syncing now, so the row reconnects cleanly.
            if order is not None and order.broker_account_id != acct.id:
                order.broker_account_id = acct.id

        if order is None:
            # Race guard: don't synthesize a duplicate for an in-flight
            # app-placed order whose row hasn't committed yet. Skip this fill
            # for now; a later sync matches it by broker_order_id (or
            # synthesizes it once it's clearly external — older than the grace
            # window). We deliberately DON'T add activity_id to `existing`, so
            # the next sync re-processes it.
            if (datetime.now(timezone.utc) - trade_at) < _SYNTH_ORDER_GRACE:
                skipped += 1
                continue
            # Already recorded for ANOTHER user on this same brokerage account
            # (a subscriber's mirror, or the other holder's own trade): not ours.
            # Synthesizing it gave a trader a phantom order that the listener's
            # backfill then fanned out, and double-counted shared-account P&L.
            if order_intent.is_mirror_echo(db, acct.user_id, acct.id, broker_oid, None):
                skipped += 1
                continue
            order = Order(
                user_id=acct.user_id,
                broker_account_id=acct.id,
                instrument_type=instrument,
                symbol=display_symbol,
                option_expiry=option_expiry,
                option_strike=option_strike,
                option_right=option_right,
                side=side,
                order_type=OrderType.MARKET,
                quantity=units,
                status=OrderStatus.FILLED,
                broker_order_id=broker_oid or activity_id,
                filled_quantity=units,
                filled_avg_price=price,
                submitted_at=trade_at,
                closed_at=trade_at,
                broker_filled_at=trade_at,
            )
            # Insert inside a savepoint: if a concurrent sync/listener already
            # recorded this exact FILLED order (unique index on
            # user_id+broker_order_id WHERE status=FILLED), adopt the winner's
            # row instead of duplicating. The fill below dedups on broker_fill_id.
            try:
                with db.begin_nested():
                    db.add(order)
                    db.flush()
                orders_added += 1
            except IntegrityError:
                db.expunge(order)
                order = db.execute(
                    select(Order).where(
                        Order.user_id == acct.user_id,
                        Order.broker_order_id == (broker_oid or activity_id),
                        Order.status == OrderStatus.FILLED,
                    ).order_by(Order.created_at.asc()).limit(1)
                ).scalar_one_or_none()
                if order is None:
                    skipped += 1
                    continue
        else:
            # Recompute filled qty + volume-weighted avg from the AUTHORITATIVE
            # Fill rows (deduped by broker_fill_id), rather than ACCUMULATING
            # onto order.filled_quantity. Accumulating double-counted any order
            # whose place response / listener had already set filled_quantity:
            # e.g. an Alpaca market order that fills instantly is created with
            # filled_quantity=1, then this fill activity added another 1 → 2.
            # Summing the existing Fill rows (which exclude the one we're about
            # to add) plus `units` gives the true cumulative fill.
            prior_qty, prior_notional = db.execute(
                select(
                    func.coalesce(func.sum(Fill.quantity), 0),
                    func.coalesce(func.sum(Fill.quantity * Fill.price), 0),
                ).where(Fill.order_id == order.id)
            ).one()
            prior_qty = _dec(prior_qty)
            prior_notional = _dec(prior_notional)
            new_qty = prior_qty + units
            new_notional = prior_notional + units * price
            # Clamp to the order quantity — you can never fill MORE than you
            # ordered. Guards against any residual duplicate fill activities.
            order_qty = _dec(order.quantity)
            order.filled_quantity = min(new_qty, order_qty) if order_qty > 0 else new_qty
            order.filled_avg_price = (new_notional / new_qty) if new_qty > 0 else price
            # Mark filled if this completes the order quantity; otherwise
            # partial-fill. Float-tolerant compare (Decimal).
            if new_qty >= _dec(order.quantity):
                order.status = OrderStatus.FILLED
                order.closed_at = trade_at
                # Alpaca's activities feed carries the venue's own
                # transaction_time, so this path has always had the real fill
                # time — it just lived only on the Fill rows. Mirror it onto the
                # order so ONE column answers "when did this fill" for every
                # broker, instead of the frontend having to know that Alpaca
                # alone has fills to look at.
                if order.broker_filled_at is None:
                    order.broker_filled_at = trade_at
            else:
                order.status = OrderStatus.PARTIALLY_FILLED

        fill = Fill(
            order_id=order.id,
            quantity=units,
            price=price,
            fee=Decimal(0),
            filled_at=trade_at,
            broker_fill_id=activity_id,
        )
        # Insert inside a savepoint so a concurrent sync that already recorded
        # this fill (unique broker_fill_id) just skips it here instead of failing
        # the whole batch commit.
        try:
            with db.begin_nested():
                db.add(fill)
                db.flush()
        except IntegrityError:
            skipped += 1
            existing.add(activity_id)
            continue
        fills_added += 1
        existing.add(activity_id)

    acct.last_activity_sync_at = datetime.now(timezone.utc)

    # Discord broadcast — sweep this trader's recently-FILLED orders and emit any
    # that haven't been alerted yet (path-independent backstop: catches fills this
    # sync — or ANY prior sync/socket drop — completed without a card). Only for a
    # trader with Discord on. Commit FIRST so the emit threads (own sessions) see
    # the committed FILLED rows; callers commit right after, so this is a safe
    # no-op for them. Never let an alert failure break the sync.
    try:
        from app.models.settings import TraderSettings  # noqa: PLC0415
        from app.services import discord_alerts  # noqa: PLC0415
        _ts = db.get(TraderSettings, acct.user_id)
        if _ts is not None and _ts.discord_alerts_enabled and _ts.discord_webhook_url:
            db.commit()
            discord_alerts.emit_pending_trader_alerts(db, acct.user_id)
    except Exception:  # noqa: BLE001
        log.exception("fills_sync: discord alert sweep failed")

    return SyncResult(
        fills_added=fills_added,
        orders_added=orders_added,
        activities_seen=len(activities),
        skipped=skipped,
    )


# A user's sync is claimed in Redis for its whole run, broker reads included,
# so concurrent page loads skip it without holding a DB connection. Outlives a
# crashed holder by at most this long.
_CLAIM_TTL_S = 120
_RELEASE_CLAIM = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) end return 0"
)
_IN_PROGRESS = {"per_account": [], "fills_added": 0, "orders_added": 0, "in_progress": True}


def _claim_key(user_id: uuid.UUID) -> str:
    return f"fills_sync:claim:{user_id}"


def _claim_sync(user_id: uuid.UUID) -> "tuple[bool, str | None]":
    """(False, None): another sync for this user is running. Redis down →
    (True, None): run unclaimed; the write-phase advisory lock still keeps
    two syncs from writing the same rows."""
    from app.services.redis_client import get_sync_redis  # noqa: PLC0415

    token = uuid.uuid4().hex
    try:
        if get_sync_redis().set(_claim_key(user_id), token, nx=True, ex=_CLAIM_TTL_S):
            return True, token
        return False, None
    except Exception:  # noqa: BLE001
        log.debug("fills_sync: redis claim unavailable", exc_info=True)
        return True, None


def _release_claim(user_id: uuid.UUID, token: str) -> None:
    from app.services.redis_client import get_sync_redis  # noqa: PLC0415

    try:
        get_sync_redis().eval(_RELEASE_CLAIM, 1, _claim_key(user_id), token)
    except Exception:  # noqa: BLE001
        log.debug("fills_sync: redis claim release failed", exc_info=True)


class _Prefetched:
    """An account's broker reads, taken before the write phase opens a
    transaction, served to _refresh_open_orders / sync_account_fills from
    memory."""

    def __init__(self, orders: "dict[str, Any]", activities: list):
        self._orders = orders
        self._activities = activities

    def get_orders_snapshot(self) -> "dict[str, Any]":
        return self._orders

    def get_order(self, broker_order_id: str) -> Any:
        if broker_order_id not in self._orders:
            raise LookupError(f"order {broker_order_id} not read this sync")
        return self._orders[broker_order_id]

    def list_recent_activities(self) -> list:
        return self._activities


def _prefetch(adapter: Any, open_ids: "list[str]") -> _Prefetched:
    # Same reads the inline sweep made: one snapshot when there is more than
    # one order to check, get_order for anything it missed.
    orders: dict[str, Any] = {}
    batch = getattr(adapter, "get_orders_snapshot", None)
    if len(open_ids) > 1 and batch is not None:
        try:
            orders.update(batch() or {})
        except Exception:  # noqa: BLE001
            log.warning("fills_sync: batch order snapshot failed — per-order reads", exc_info=True)
    for boid in open_ids:
        if boid not in orders:
            try:
                orders[boid] = adapter.get_order(boid)
            except Exception:  # noqa: BLE001
                continue  # stale/cancelled id — as before, not worth failing the sync
    return _Prefetched(orders, adapter.list_recent_activities())


def _account_row(acct: BrokerAccount, res: "SyncResult | None" = None,
                 exc: "Exception | None" = None) -> dict[str, Any]:
    row: dict[str, Any] = {"broker_account_id": str(acct.id), "broker": acct.broker.value}
    if exc is not None:
        row["error"] = str(exc)[:300]
    elif res is not None:
        row.update(fills_added=res.fills_added, orders_added=res.orders_added, skipped=res.skipped)
    return row


def sync_user_fills(
    db: Session, user_id: uuid.UUID, min_interval: timedelta | None = None,
) -> dict[str, Any]:
    """Sync every connected broker for one app user.

    ``min_interval`` skips an account synced more recently than that (by any
    process — the worker's poller stamps the same column).

    Ends ``db``'s open transaction: the broker reads run with no pooled
    connection checked out, between a short read and a short write."""
    # One fills-sync per user at a time. Every calendar and positions load
    # triggers this; a second one skips rather than waits (QA 2026-10-21: page
    # loads queued behind a slow sync ran the pool dry).
    claimed, token = _claim_sync(user_id)
    if not claimed:
        log.info("fills_sync: sync for user=%s already running — skipped", user_id)
        return dict(_IN_PROGRESS)
    try:
        return _sync_user_fills(db, user_id, min_interval)
    finally:
        if token is not None:
            _release_claim(user_id, token)


def _sync_user_fills(db: Session, user_id: uuid.UUID, min_interval: timedelta | None) -> dict[str, Any]:
    accts = list(db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user_id,
            # An inactive (paused) broker is left untouched until reactivated.
            BrokerAccount.connection_status != "inactive",
        )
    ).scalars())

    per_account: list[dict[str, Any]] = []
    plans: list[tuple[BrokerAccount, Any, list[str]]] = []
    now = datetime.now(timezone.utc)
    for acct in accts:
        last = acct.last_activity_sync_at
        if min_interval is not None and last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < min_interval:
                continue
        try:
            adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
        except Exception as exc:  # noqa: BLE001
            per_account.append(_account_row(acct, exc=exc))
            continue
        if not isinstance(adapter, AlpacaAdapter):
            per_account.append(_account_row(acct, SyncResult(0, 0, 0, 0)))
            continue
        open_ids = list(db.execute(
            select(Order.broker_order_id).where(
                Order.broker_account_id == acct.id,
                Order.status.in_(_NON_TERMINAL_STATUSES),
                Order.broker_order_id.isnot(None),
            )
        ).scalars())
        plans.append((acct, adapter, open_ids))

    fetched: list[tuple[BrokerAccount, _Prefetched]] = []
    if plans:
        release_connection(db)
        for acct, adapter, open_ids in plans:
            try:
                fetched.append((acct, _prefetch(adapter, open_ids)))
            except Exception as exc:  # noqa: BLE001
                per_account.append(_account_row(acct, exc=exc))

    total_fills = 0
    total_orders = 0
    if fetched:
        # The check-then-insert that synthesizes external orders must not run
        # twice at once — duplicate FILLED rows double-count realized P&L. The
        # Redis claim normally guarantees that; this covers Redis being down.
        # Only DB work happens under it, so it is held for milliseconds.
        got = db.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
                         {"k": f"fills_sync:{user_id}"}).scalar()
        if not got:
            log.info("fills_sync: another sync for user=%s is writing — skipped", user_id)
            return dict(_IN_PROGRESS)
        db.execute(text("SET LOCAL idle_in_transaction_session_timeout = '60s'"))
        for acct, pre in fetched:
            try:
                res = sync_account_fills(db, acct, pre)
            except Exception as exc:  # noqa: BLE001
                per_account.append(_account_row(acct, exc=exc))
                continue
            per_account.append(_account_row(acct, res))
            total_fills += res.fills_added
            total_orders += res.orders_added

    return {
        "per_account": per_account,
        "fills_added": total_fills,
        "orders_added": total_orders,
    }
