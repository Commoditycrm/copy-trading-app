import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import client_ip, current_user, require_trader
from app.brokers import BrokerOrderRequest, adapter_for
from app.database import SessionLocal, get_db
from app.models.broker_account import BrokerAccount
from app.models.order import Order, OrderSide, OrderStatus
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole
from app.schemas.order import CloseOrderIn, DailyPnL, OrderOut, PlaceOrderIn
from app.services import audit, copy_engine, events, fills_sync
from app.services.crypto import decrypt_json
from app.services.pnl import realized_pnl_by_day

router = APIRouter(prefix="/api", tags=["trades"])


@router.get("/trades", response_model=list[OrderOut])
def list_trades(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    limit: int = Query(default=200, le=1000),
) -> list[Order]:
    q = (
        select(Order)
        .options(selectinload(Order.fills))
        .where(Order.user_id == user.id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    if from_:
        q = q.where(Order.created_at >= datetime.combine(from_, datetime.min.time(), tzinfo=timezone.utc))
    if to:
        q = q.where(Order.created_at < datetime.combine(to, datetime.min.time(), tzinfo=timezone.utc))
    return list(db.execute(q).scalars())


@router.get("/trades/{order_id}", response_model=OrderOut)
def get_trade(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Order:
    order = db.execute(
        select(Order).options(selectinload(Order.fills)).where(Order.id == order_id)
    ).scalar_one_or_none()
    if not order or order.user_id != user.id:
        raise HTTPException(404, "not_found")
    return order


_CANCELLABLE_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)


def _run_cancel_fanout_in_background(trader_order_id: uuid.UUID) -> None:
    """When a trader cancels their root order, cascade-cancel every still-open
    subscriber mirror at the subscriber's broker. Runs after the trader's HTTP
    response is sent. Per-mirror failures are audited, not raised."""
    with SessionLocal() as db:
        children = list(db.execute(
            select(Order).where(
                Order.parent_order_id == trader_order_id,
                Order.status.in_(_CANCELLABLE_STATUSES),
            )
        ).scalars())
        if not children:
            return

        pending: list[tuple[Order, object]] = []  # (child, adapter)
        for child in children:
            if not child.broker_order_id:
                # Never made it to the broker — just mark cancelled locally.
                child.status = OrderStatus.CANCELED
                child.closed_at = datetime.now(timezone.utc)
                continue
            acct = db.get(BrokerAccount, child.broker_account_id)
            if acct is None:
                child.status = OrderStatus.CANCELED
                child.closed_at = datetime.now(timezone.utc)
                continue
            try:
                creds = decrypt_json(acct.encrypted_credentials)
                adapter = adapter_for(acct, creds)
            except Exception as exc:  # noqa: BLE001
                audit.record(
                    db, actor_user_id=child.user_id, action="order.mirror_cancel_creds_error",
                    entity_type="order", entity_id=child.id,
                    metadata={"parent_order_id": str(trader_order_id), "error": str(exc)[:300]},
                )
                child.status = OrderStatus.CANCELED
                child.closed_at = datetime.now(timezone.utc)
                continue
            pending.append((child, adapter))

        def _cancel(item: tuple[Order, object]) -> tuple[Order, str | None]:
            ch, ad = item
            try:
                ad.cancel_order(ch.broker_order_id)  # type: ignore[attr-defined]
                return ch, None
            except Exception as exc:  # noqa: BLE001
                return ch, str(exc)[:300]

        if pending:
            with ThreadPoolExecutor(max_workers=min(32, len(pending))) as pool:
                results = list(pool.map(_cancel, pending))
            for child, err in results:
                # Re-fetch through the session in case SQLAlchemy needs it.
                ch = db.get(Order, child.id)
                if ch is None:
                    continue
                if err is None:
                    ch.status = OrderStatus.CANCELED
                    ch.closed_at = datetime.now(timezone.utc)
                    audit.record(
                        db, actor_user_id=ch.user_id, action="order.mirror_cancelled",
                        entity_type="order", entity_id=ch.id,
                        metadata={
                            "parent_order_id": str(trader_order_id),
                            "broker_order_id": ch.broker_order_id,
                        },
                    )
                    events.publish(ch.user_id, copy_engine._order_event("order.cancelled", ch))
                else:
                    # Broker rejected (e.g. mirror already filled before we got
                    # to it). Don't mutate status — sync-fills will reconcile.
                    audit.record(
                        db, actor_user_id=ch.user_id, action="order.mirror_cancel_failed",
                        entity_type="order", entity_id=ch.id,
                        metadata={
                            "parent_order_id": str(trader_order_id),
                            "broker_order_id": ch.broker_order_id,
                            "error": err,
                        },
                    )
        db.commit()


async def _run_rejection_notify_in_background(
    trader_order_id: uuid.UUID, trader_id: uuid.UUID
) -> None:
    """Notify subscribers when the TRADER's entry order was rejected at
    the broker — so they see "the trader tried X and it was rejected"
    instead of silence.

    Runs after the 502 response is sent. Per-subscriber failure is
    isolated (one bad row doesn't kill the loop) and the whole thing
    is best-effort: if Redis / DB hiccup mid-fanout we log and move on
    rather than retrying — a missed notification is preferable to a
    duplicated one.
    """
    from app.services import notifications as notif_svc  # noqa: PLC0415

    with SessionLocal() as db:
        order = db.get(Order, trader_order_id)
        trader = db.get(User, trader_id)
        if order is None or trader is None:
            return

        # Pull the subscriber list the same way the regular fanout does —
        # so a subscriber who has copy paused doesn't get spammed about
        # rejections they wouldn't have received anyway. The cache helper
        # filters out paused/disabled rows for us.
        try:
            subs = await copy_engine.cache.get_subscribers_for_trader(db, trader.id)
        except Exception:  # noqa: BLE001
            log = __import__("logging").getLogger(__name__)
            log.exception("rejection-notify: subscriber lookup failed")
            return

        if not subs:
            return

        trader_label = trader.display_name or trader.email or "Trader"
        symbol = order.symbol or "—"
        side = order.side.value.upper() if order.side else "?"
        qty = str(order.quantity) if order.quantity is not None else "?"
        reason = (order.reject_reason or "broker rejected").strip()
        # Keep the message concise — the notifications bell has limited
        # real estate. Detail goes in metadata for the deep-dive view.
        message = (
            f"{trader_label} tried to {side} {qty} {symbol} — rejected by broker"
        )
        metadata = {
            "trader_id": str(trader.id),
            "trader_order_id": str(order.id),
            "symbol": symbol,
            "side": order.side.value if order.side else None,
            "order_type": order.order_type.value if order.order_type else None,
            "quantity": qty,
            "limit_price": str(order.limit_price) if order.limit_price else None,
            "stop_price": str(order.stop_price) if order.stop_price else None,
            "reason": reason[:300],
        }

        for sub in subs:
            try:
                notif_svc.create_notification(
                    db,
                    user_id=sub.user_id,
                    type="trader.order_rejected",
                    message=message,
                    metadata=metadata,
                )
            except Exception:  # noqa: BLE001
                log = __import__("logging").getLogger(__name__)
                log.exception(
                    "rejection-notify: failed for subscriber=%s order=%s",
                    sub.user_id, order.id,
                )
        db.commit()


async def _run_fanout_in_background(trader_order_id: uuid.UUID, trader_id: uuid.UUID) -> None:
    """Runs after the response is sent. Async so we can fan out 200 broker
    calls concurrently on the same event loop. Opens its own DB session
    because the request-scoped session is closed by the time this fires."""
    with SessionLocal() as db:
        order = db.get(Order, trader_order_id)
        trader = db.get(User, trader_id)
        if order is None or trader is None:
            return
        fan_results = await copy_engine.fanout_async(db, order, trader)
        audit.record(
            db,
            actor_user_id=trader.id,
            action="trader.fanout_complete",
            entity_type="order",
            entity_id=order.id,
            metadata={
                "subscriber_count": len({r.subscriber_user_id for r in fan_results}),
                "submitted": sum(1 for r in fan_results if r.status == "submitted"),
                "errors": sum(1 for r in fan_results if r.status == "error"),
                "skipped": sum(1 for r in fan_results if r.status.startswith("skipped")),
            },
        )
        db.commit()


def _place_trader_order(
    db: Session,
    trader: User,
    payload: PlaceOrderIn,
    broker_account_id: uuid.UUID,
    background: BackgroundTasks,
    request: Request,
    skip_fanout: bool = False,
) -> Order:
    """Core order-placement flow. Used by /api/trades for trader-originated
    orders (which fan out to subscribers) and by close endpoints. Also reused
    for subscriber-originated closes — in that case we skip the trader
    kill-switch check and don't fan anything out.

    Returns the persisted Order. Caller commits nothing — this function
    commits before returning.
    """
    is_trader = trader.role == UserRole.TRADER
    # Trader kill switch only applies to traders. Subscribers can always
    # manage (close/cancel) their own broker accounts.
    if is_trader and not copy_engine.trader_can_trade(db, trader):
        raise HTTPException(409, "trading_disabled")

    acct = db.get(BrokerAccount, broker_account_id)
    if not acct or acct.user_id != trader.id:
        raise HTTPException(404, "broker_account_not_found")
    if acct.connection_status != "connected":
        raise HTTPException(409, "broker_not_connected")
    creds = decrypt_json(acct.encrypted_credentials)

    # Will this order be broadcast to subscribers? Pre-compute so we can
    # stamp the flag on the row at creation time (immutable record of intent).
    from app.models.settings import TraderSettings  # local import — avoid cycle
    ts = db.get(TraderSettings, trader.id) if is_trader else None
    will_fanout = is_trader and not skip_fanout and not (ts and ts.copy_paused)

    # Lifecycle: the moment the trader's submit hit our backend. Used by the
    # Performance page to compute api_to_broker_lag (= broker_accepted_at -
    # trader_submitted_at).
    trader_submitted_at = datetime.now(timezone.utc)

    # Server-side duplicate suppression. A short window (3 seconds) catches
    # accidental double-POSTs from any source — React StrictMode dev re-fires,
    # browser request retries, click + Enter races, the api() helper's 401
    # retry, etc. — without rejecting legitimate "trader just placed the same
    # trade twice on purpose" cases (no human re-clicks the same exact order
    # in 3s). We match on the full identity of the order (broker, symbol,
    # side, qty, type, prices, option params) so two different orders at the
    # same moment don't collide.
    from datetime import timedelta  # noqa: PLC0415
    DEDUP_WINDOW = timedelta(seconds=3)
    cutoff = trader_submitted_at - DEDUP_WINDOW
    existing = db.execute(
        select(Order).where(
            Order.user_id == trader.id,
            Order.broker_account_id == acct.id,
            Order.parent_order_id.is_(None),                # not a subscriber mirror
            Order.bracket_parent_id.is_(None),              # not an emulator-placed exit
            Order.instrument_type == payload.instrument_type,
            Order.symbol == payload.symbol.upper(),
            Order.side == payload.side,
            Order.order_type == payload.order_type,
            Order.quantity == payload.quantity,
            Order.limit_price == payload.limit_price,
            Order.stop_price == payload.stop_price,
            Order.option_expiry == payload.option_expiry,
            Order.option_strike == payload.option_strike,
            Order.option_right == payload.option_right,
            Order.created_at >= cutoff,
        ).order_by(Order.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        import logging  # noqa: PLC0415
        logging.getLogger(__name__).warning(
            "trades: duplicate suppressed for user=%s symbol=%s side=%s qty=%s "
            "within %ss window (returning existing order %s)",
            trader.id, payload.symbol, payload.side.value, payload.quantity,
            DEDUP_WINDOW.total_seconds(), existing.id,
        )
        return existing

    order = Order(
        user_id=trader.id,
        broker_account_id=acct.id,
        instrument_type=payload.instrument_type,
        symbol=payload.symbol.upper(),
        option_expiry=payload.option_expiry,
        option_strike=payload.option_strike,
        option_right=payload.option_right,
        side=payload.side,
        order_type=payload.order_type,
        quantity=payload.quantity,
        limit_price=payload.limit_price,
        stop_price=payload.stop_price,
        take_profit_price=payload.take_profit_price,
        stop_loss_price=payload.stop_loss_price,
        status=OrderStatus.PENDING,
        fanned_out_to_subscribers=will_fanout,
        trader_submitted_at=trader_submitted_at,
    )
    db.add(order)
    db.flush()

    adapter = adapter_for(acct, creds)
    # Forward bracket prices to the adapter ONLY when the (adapter,
    # instrument) pair supports native bracket / OCO. Today that's
    # Alpaca STOCKS only — Alpaca's options API explicitly rejects
    # complex orders (error 42210000 "complex orders not supported for
    # options trading"). For everything else (SnapTrade, Webull, IBKR,
    # AND Alpaca options) we keep TP/SL on the Order row but place the
    # entry plain — the bracket_emulator service then places the exit
    # legs when the listener detects the entry has filled. See
    # app/services/bracket_emulator.py.
    from app.brokers.alpaca import AlpacaAdapter  # noqa: PLC0415
    from app.models.order import InstrumentType  # noqa: PLC0415
    use_native_bracket = (
        isinstance(adapter, AlpacaAdapter)
        and order.instrument_type != InstrumentType.OPTION
    )
    _broker_t0 = time.perf_counter()
    try:
        result = adapter.place_order(
            BrokerOrderRequest(
                instrument_type=order.instrument_type,
                symbol=order.symbol,
                side=order.side,
                order_type=order.order_type,
                quantity=order.quantity,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                take_profit_price=order.take_profit_price if use_native_bracket else None,
                stop_loss_price=order.stop_loss_price if use_native_bracket else None,
                option_expiry=order.option_expiry,
                option_strike=order.option_strike,
                option_right=order.option_right,
                client_order_id=str(order.id),
            )
        )
    except Exception as exc:  # noqa: BLE001
        order.broker_call_ms = int((time.perf_counter() - _broker_t0) * 1000)
        order.status = OrderStatus.REJECTED
        order.reject_reason = str(exc)[:480]
        order.closed_at = datetime.now(timezone.utc)
        audit.record(
            db, actor_user_id=trader.id, action="trader.order_rejected_at_broker",
            entity_type="order", entity_id=order.id,
            metadata={"error": str(exc)[:480]}, ip_address=client_ip(request),
        )
        db.commit()
        # Tell subscribers the trader tried to enter and got rejected — so
        # the rejection isn't silent on their side. Only when this order
        # would have fanned out (i.e. trader-originated AND copy not paused
        # AND `skip_fanout=False`). Runs AFTER the response is sent.
        if will_fanout:
            background.add_task(_run_rejection_notify_in_background, order.id, trader.id)
        raise HTTPException(502, f"broker_error: {exc}")

    # Perf instrumentation: broker place-order round-trip (request->response)
    # and when the broker accepted. broker_call_ms was previously never
    # populated, so the Performance page's broker-latency column was always
    # blank — now it reflects the real broker call time.
    order.broker_call_ms = int((time.perf_counter() - _broker_t0) * 1000)
    order.broker_accepted_at = datetime.now(timezone.utc)
    order.broker_order_id = result.broker_order_id
    order.status = result.status
    order.submitted_at = result.submitted_at
    order.filled_quantity = result.filled_quantity
    order.filled_avg_price = result.filled_avg_price

    audit.record(
        db, actor_user_id=trader.id, action="trader.order_placed",
        entity_type="order", entity_id=order.id,
        metadata={
            "broker": acct.broker, "symbol": order.symbol, "side": order.side.value,
            "qty": str(order.quantity), "broker_order_id": result.broker_order_id,
        },
        ip_address=client_ip(request),
    )

    # Lifecycle: stamp the broadcast time before publishing so the row
    # carries the timestamp the very first time it surfaces in the API.
    order.redis_published_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)

    events.publish(trader.id, copy_engine._order_event("order.placed", order))
    # Only trader-originated orders fan out to subscribers. Subscribers placing
    # their own close don't propagate to anyone. Callers (e.g. close-all with
    # "mine only" scope) can also opt out via skip_fanout. The trader's master
    # pause is also checked at the start of fanout itself — we record the
    # intended-fanout flag (`will_fanout`) on the order row above.
    if will_fanout:
        background.add_task(_run_fanout_in_background, order.id, trader.id)
    return order


@router.post("/trades", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def place_trade(
    payload: PlaceOrderIn,
    request: Request,
    background: BackgroundTasks,
    broker_account_id: uuid.UUID = Query(..., description="Trader's broker account to place on"),
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
) -> Order:
    return _place_trader_order(db, trader, payload, broker_account_id, background, request)


@router.post("/trades/{order_id}/cancel", response_model=OrderOut)
def cancel_trade(
    order_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Order:
    """Cancel an open order at the broker. Any user can cancel their own
    orders (subscriber's mirror or trader's own). Cancellable statuses:
    PENDING, SUBMITTED, ACCEPTED, PARTIALLY_FILLED."""
    order = db.execute(
        select(Order).options(selectinload(Order.fills)).where(Order.id == order_id)
    ).scalar_one_or_none()
    if not order or order.user_id != user.id:
        raise HTTPException(404, "not_found")
    if order.status not in (
        OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED
    ):
        raise HTTPException(409, f"not_cancellable: status is {order.status.value}")

    acct = db.get(BrokerAccount, order.broker_account_id)
    if acct is None:
        raise HTTPException(404, "broker_account_missing")

    # Best-effort broker call. If the broker rejects (e.g. order already filled),
    # surface the error but DON'T mutate local state — DB stays accurate.
    if order.broker_order_id:
        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter_for(acct, creds).cancel_order(order.broker_order_id)
        except Exception as exc:  # noqa: BLE001
            audit.record(
                db, actor_user_id=user.id, action="order.cancel_failed",
                entity_type="order", entity_id=order.id,
                metadata={"error": str(exc)[:480]}, ip_address=client_ip(request),
            )
            db.commit()
            raise HTTPException(502, f"broker_error: {exc}")

    order.status = OrderStatus.CANCELED
    order.closed_at = datetime.now(timezone.utc)
    audit.record(
        db, actor_user_id=user.id, action="order.cancelled",
        entity_type="order", entity_id=order.id,
        metadata={"broker_order_id": order.broker_order_id},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(order)
    events.publish(user.id, copy_engine._order_event("order.cancelled", order))

    # If a trader cancels their own root order, cascade the cancel to every
    # open subscriber mirror. Subscribers cancelling their own mirror skip
    # this — there are no children to propagate to.
    if order.parent_order_id is None and user.role == UserRole.TRADER:
        background.add_task(_run_cancel_fanout_in_background, order.id)

    return order


@router.post("/trades/cancel-all-open")
def cancel_all_open_orders(
    request: Request,
    background: BackgroundTasks,
    include_subscribers: bool = Query(
        default=True,
        description=(
            "Trader-only knob. When True, after cancelling each of the "
            "trader's root orders we cascade the cancel to every "
            "subscriber's mirror order (same path as the single-order "
            "cancel endpoint). When False, only the trader's own root "
            "orders are cancelled and mirror orders are left alone — "
            "useful when the trader wants to clean up their own queue "
            "without yanking trades subscribers may still want filled. "
            "Ignored when the caller is a subscriber (they have no "
            "downstream to fan out to)."
        ),
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Cancel every open order owned by the caller.

    Open = status in (PENDING, SUBMITTED, ACCEPTED, PARTIALLY_FILLED).
    Mirror-the-shape of /api/positions/close-all so the Exit-All UI can
    route to either endpoint based on the user's first choice (orders
    vs. positions). Per-order broker failures don't abort the rest —
    we surface a count + list so the UI can hint at partial success.
    """
    orders = list(db.execute(
        select(Order)
        .options(selectinload(Order.fills))
        .where(Order.user_id == user.id, Order.status.in_(_CANCELLABLE_STATUSES))
    ).scalars())

    cancelled_ids: list[uuid.UUID] = []
    failed: list[dict] = []

    # We cancel sequentially. The N here is bounded (a trader rarely has
    # >50 open orders); parallel cancel adds complexity (per-broker
    # concurrency limits, lock contention on adapter sessions) without
    # meaningful win for the typical case. If volume grows we can revisit
    # using the same ThreadPoolExecutor pattern as _run_cancel_fanout.
    for order in orders:
        acct = db.get(BrokerAccount, order.broker_account_id) if order.broker_account_id else None
        try:
            if acct is not None and order.broker_order_id:
                creds = decrypt_json(acct.encrypted_credentials)
                adapter_for(acct, creds).cancel_order(order.broker_order_id)
        except Exception as exc:  # noqa: BLE001
            # Broker rejected the cancel (often "order already filled" or
            # "no such order" — both fine; DB will reflect status on next
            # listener update). Record + skip rather than mutate state.
            audit.record(
                db, actor_user_id=user.id, action="order.cancel_failed",
                entity_type="order", entity_id=order.id,
                metadata={"error": str(exc)[:480], "via": "cancel-all-open"},
                ip_address=client_ip(request),
            )
            failed.append({
                "order_id": str(order.id),
                "symbol":   order.symbol,
                "error":    str(exc)[:300],
            })
            continue

        order.status = OrderStatus.CANCELED
        order.closed_at = datetime.now(timezone.utc)
        audit.record(
            db, actor_user_id=user.id, action="order.cancelled",
            entity_type="order", entity_id=order.id,
            metadata={"via": "cancel-all-open",
                      "broker_order_id": order.broker_order_id},
            ip_address=client_ip(request),
        )
        cancelled_ids.append(order.id)

    db.commit()

    # Publish SSE *after* commit so subscribers reading from the DB on
    # event-receipt see the cancelled state.
    for oid in cancelled_ids:
        cancelled = db.get(Order, oid)
        if cancelled is None:
            continue
        events.publish(user.id, copy_engine._order_event("order.cancelled", cancelled))

    # Trader-only: cascade cancel to mirror orders for every root order
    # we just cancelled. Skip the cascade if caller asked for "just me"
    # OR is a subscriber (subscribers have no downstream).
    if user.role == UserRole.TRADER and include_subscribers:
        for oid in cancelled_ids:
            cancelled = db.get(Order, oid)
            if cancelled is not None and cancelled.parent_order_id is None:
                background.add_task(_run_cancel_fanout_in_background, oid)
    elif user.role == UserRole.TRADER and not include_subscribers:
        # The trader explicitly said "just my orders". Drop a Redis marker
        # for each cancelled root order so the broker listener — which
        # receives the canceled WebSocket/poll event AFTER our cancel —
        # knows to skip ITS cascade too. Without this, the listener sees
        # `fanned_out_to_subscribers=True` and runs the same mirror-cancel
        # cascade we just deliberately avoided, defeating the toggle.
        from app.services.cancel_intent import mark_no_cascade  # noqa: PLC0415
        for oid in cancelled_ids:
            cancelled = db.get(Order, oid)
            if cancelled is not None and cancelled.parent_order_id is None:
                mark_no_cascade(oid)

    return {
        "cancelled_count": len(cancelled_ids),
        "failed_count":    len(failed),
        "failed":          failed,
    }


@router.post("/trades/cancel-all-subscribers-open")
async def cancel_all_subscribers_open_orders(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> dict:
    """Trader-only: cancel every open order across EVERY subscriber
    following this trader. The trader's OWN orders are not touched.

    Returns IMMEDIATELY with the queued count — the actual broker
    cancellations run in the background. This matters because a trader
    can easily have hundreds or thousands of open subscriber orders
    accumulated over a session (one observed 1,980), and a synchronous
    sweep at SnapTrade's rate limit would exceed the request timeout
    long before completing.

    Each completed cancel publishes an ``order.cancelled`` SSE event
    so the Order History page updates live. Failures (broker rejects,
    timeouts) audit but never crash the background task — the rest
    keep flowing.
    """
    import asyncio  # noqa: PLC0415

    sub_ids = list(db.execute(
        select(SubscriberSettings.user_id).where(
            SubscriberSettings.following_trader_id == user.id
        )
    ).scalars())
    if not sub_ids:
        return {"queued_count": 0, "message": "No subscribers."}

    # Snapshot the orders + every field the workers need. We can't
    # share the request DB session across the background task because
    # FastAPI closes it the moment we return.
    rows = db.execute(
        select(
            Order.id, Order.broker_account_id, Order.broker_order_id,
            Order.symbol, Order.user_id,
        ).where(
            Order.user_id.in_(sub_ids), Order.status.in_(_CANCELLABLE_STATUSES),
        )
    ).all()
    if not rows:
        return {"queued_count": 0, "message": "No subscriber orders to cancel."}

    targets = [
        {
            "order_id": r.id, "acct_id": r.broker_account_id,
            "broker_order_id": r.broker_order_id, "symbol": r.symbol,
            "user_id": r.user_id,
        }
        for r in rows
    ]
    trader_id = user.id
    client_ip_str = client_ip(request)

    # Spawn the actual work on the running event loop. asyncio.create_task
    # is fire-and-forget — the response goes out immediately while the
    # task runs in parallel. We don't await it.
    asyncio.create_task(
        _bulk_cancel_subscriber_orders_background(
            targets, trader_id, client_ip_str,
        )
    )

    return {
        "queued_count": len(targets),
        "message": (
            f"Queued {len(targets)} subscriber order(s) for cancellation. "
            "Order History will update live as each completes."
        ),
    }


# Timeout + concurrency for bulk-exit operations.
#
# Concurrency = 4: SnapTrade's 250 req/min platform quota means we can
# sustain ~4 req/sec across all our SnapTrade traffic. The order
# listener is already burning ~12-24 req/min for traders; leaving 4
# concurrent cancels gives us room to do bulk work without 429-ing.
# Alpaca tolerates much more concurrency but this code path is shared
# and SnapTrade is the bottleneck.
#
# Timeout = 60s: covers SnapTrade slow paths during throttling. A
# timeout doesn't mean the cancel failed — the broker may have
# processed it; the listener will reconcile on its next poll.
_BULK_EXIT_BROKER_TIMEOUT_S = 60.0
_BULK_EXIT_CONCURRENCY = 4


async def _bulk_cancel_subscriber_orders_background(
    targets: list[dict],
    trader_id: uuid.UUID,
    client_ip_str: str | None,
) -> None:
    """Background coroutine for bulk-cancel-subscribers-open.

    Runs on the main event loop after the API response is already out.
    Concurrency = ``_BULK_EXIT_CONCURRENCY``; each cancel call wrapped
    in ``_BULK_EXIT_BROKER_TIMEOUT_S``. Per-order outcomes commit
    individually + publish SSE so the UI can update incrementally —
    waiting to commit the whole batch at the end would make the
    Order History page sit stale for the duration of the run.
    """
    import asyncio  # noqa: PLC0415
    import logging  # noqa: PLC0415
    log = logging.getLogger(__name__)
    log.info(
        "bulk-cancel-subscribers: starting background sweep of %d order(s) "
        "for trader=%s (concurrency=%d, per-call timeout=%.0fs)",
        len(targets), trader_id, _BULK_EXIT_CONCURRENCY, _BULK_EXIT_BROKER_TIMEOUT_S,
    )
    started = datetime.now(timezone.utc)
    sem = asyncio.Semaphore(_BULK_EXIT_CONCURRENCY)
    cancelled = 0
    failed = 0

    async def _process(target: dict) -> None:
        nonlocal cancelled, failed
        async with sem:
            err = await _try_cancel_then_persist(target, trader_id, client_ip_str)
        if err is None:
            cancelled += 1
        else:
            failed += 1

    await asyncio.gather(*(_process(t) for t in targets), return_exceptions=True)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    log.info(
        "bulk-cancel-subscribers: done — cancelled=%d failed=%d total=%d "
        "elapsed=%.1fs for trader=%s",
        cancelled, failed, len(targets), elapsed, trader_id,
    )


async def _try_cancel_then_persist(
    target: dict, trader_id: uuid.UUID, client_ip_str: str | None,
) -> str | None:
    """One order: cancel at broker (with timeout) → commit local
    status + audit → publish SSE. Returns error message or None.
    Runs in the background coroutine; opens its own DB session so it
    doesn't depend on anything from the original request."""
    import asyncio  # noqa: PLC0415
    import logging  # noqa: PLC0415
    log = logging.getLogger(__name__)

    loop = asyncio.get_running_loop()
    order_id: uuid.UUID = target["order_id"]
    acct_id: uuid.UUID | None = target["acct_id"]
    broker_order_id: str | None = target["broker_order_id"]
    symbol: str | None = target["symbol"]

    # Broker call (in threadpool, with timeout).
    try:
        err = await asyncio.wait_for(
            loop.run_in_executor(
                None, _cancel_one_sync, order_id, acct_id, broker_order_id,
            ),
            timeout=_BULK_EXIT_BROKER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log.warning(
            "bulk-cancel-subscribers: timeout cancelling order=%s "
            "(broker_id=%s) after %.0fs — listener will reconcile",
            order_id, broker_order_id, _BULK_EXIT_BROKER_TIMEOUT_S,
        )
        err = (
            f"broker cancel exceeded {_BULK_EXIT_BROKER_TIMEOUT_S:.0f}s timeout "
            "— may have succeeded; will reconcile on next listener tick"
        )
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "bulk-cancel-subscribers: worker crashed for order=%s", order_id,
        )
        err = str(exc)[:300]
    else:
        # err is the second value of the (order_id, err_msg) tuple. Unpack.
        if isinstance(err, tuple):
            err = err[1]

    # DB write + SSE. Own session — request session is long gone.
    try:
        with SessionLocal() as db_local:
            order_row = db_local.get(Order, order_id)
            if order_row is None:
                return err
            if err is None:
                order_row.status = OrderStatus.CANCELED
                order_row.closed_at = datetime.now(timezone.utc)
                audit.record(
                    db_local, actor_user_id=trader_id, action="order.cancelled",
                    entity_type="order", entity_id=order_id,
                    metadata={"via": "cancel-all-subscribers-open",
                              "subscriber_user_id": str(order_row.user_id),
                              "broker_order_id": broker_order_id},
                    ip_address=client_ip_str,
                )
            else:
                audit.record(
                    db_local, actor_user_id=trader_id, action="order.cancel_failed",
                    entity_type="order", entity_id=order_id,
                    metadata={"error": err[:480],
                              "via": "cancel-all-subscribers-open",
                              "subscriber_user_id": str(order_row.user_id),
                              "symbol": symbol},
                    ip_address=client_ip_str,
                )
            db_local.commit()
            # Publish SSE AFTER commit so subscribers reading on receipt
            # see the cancelled state.
            if err is None:
                db_local.refresh(order_row)
                events.publish(
                    order_row.user_id,
                    copy_engine._order_event("order.cancelled", order_row),  # noqa: SLF001
                )
    except Exception:  # noqa: BLE001
        log.exception(
            "bulk-cancel-subscribers: db/SSE persist failed for order=%s",
            order_id,
        )

    return err


def _cancel_one_sync(
    order_id: uuid.UUID,
    acct_id: uuid.UUID | None,
    broker_order_id: str | None,
) -> tuple[uuid.UUID, str | None]:
    """Synchronous worker for one broker cancel call.

    Opens its own DB session for the broker-account lookup. Returns
    (order_id, error_msg) — caller unwraps. Returning a tuple keeps
    the legacy signature for the close-positions code path which still
    uses this directly.
    """
    # No broker_order_id → nothing to cancel at the broker; the caller
    # will still mark it CANCELED locally. Same for no account.
    if not broker_order_id or not acct_id:
        return order_id, None

    try:
        with SessionLocal() as db_local:
            acct = db_local.get(BrokerAccount, acct_id)
            if acct is None:
                # Broker disconnected — let the local cancel proceed
                # (treated as success for the row's lifecycle).
                return order_id, None
            creds = decrypt_json(acct.encrypted_credentials)
            adapter_for(acct, creds).cancel_order(broker_order_id)
        return order_id, None
    except Exception as exc:  # noqa: BLE001
        return order_id, str(exc)


@router.post("/trades/{order_id}/close", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def close_trade(
    order_id: uuid.UUID,
    payload: CloseOrderIn,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Order:
    """Close a filled order by placing a reverse-side order of the same size
    (or smaller, if `quantity` is given). The reverse is itself a normal
    order — for a trader it fans out to subscribers; for a subscriber it
    just executes against their own broker.
    """
    original = db.execute(
        select(Order).where(Order.id == order_id)
    ).scalar_one_or_none()
    if not original or original.user_id != user.id:
        raise HTTPException(404, "not_found")
    if original.status != OrderStatus.FILLED:
        raise HTTPException(409, f"not_closeable: original status is {original.status.value}")
    if original.broker_account_id is None:
        # Broker was disconnected after the order filled. Can't place a
        # reverse order — no broker to send it to. UI should disable the
        # Close button for these rows.
        raise HTTPException(409, "broker_disconnected: cannot close — reconnect the broker first")

    # Reverse the side; default qty to whatever filled on the original.
    close_qty = payload.quantity if payload.quantity is not None else original.filled_quantity
    if close_qty <= 0:
        raise HTTPException(422, "quantity_must_be_positive")
    if close_qty > original.filled_quantity:
        raise HTTPException(422, "quantity_exceeds_original_filled")

    reverse_side = OrderSide.SELL if original.side == OrderSide.BUY else OrderSide.BUY

    new_payload = PlaceOrderIn(
        instrument_type=original.instrument_type,
        symbol=original.symbol,
        side=reverse_side,
        order_type=payload.order_type,
        quantity=close_qty,
        limit_price=payload.limit_price,
        stop_price=None,
        option_expiry=original.option_expiry,
        option_strike=original.option_strike,
        option_right=original.option_right,
    )

    new_order = _place_trader_order(
        db, user, new_payload, original.broker_account_id, background, request
    )

    audit.record(
        db, actor_user_id=user.id, action="order.closed",
        entity_type="order", entity_id=original.id,
        metadata={
            "closed_with_order_id": str(new_order.id),
            "close_qty": str(close_qty),
            "close_type": payload.order_type.value,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    return new_order


@router.get("/calendar/pnl", response_model=list[DailyPnL])
def calendar_pnl(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    tz: str | None = Query(
        default=None,
        description="IANA timezone (e.g. 'Asia/Calcutta'). Fills are bucketed by this TZ so the calendar matches what the user sees as 'today'. Defaults to US/Eastern when omitted.",
    ),
    user_id: uuid.UUID | None = Query(
        default=None,
        description="Trader-only: view another user's P&L (must be a subscriber following you).",
    ),
) -> list[DailyPnL]:
    if from_ > to:
        raise HTTPException(422, "from must be <= to")

    # View-as: trader can request a subscriber's calendar. Subscribers can
    # only view their own.
    target_user_id = user.id
    if user_id is not None and user_id != user.id:
        if user.role != UserRole.TRADER:
            raise HTTPException(403, "trader_only")
        sub = db.get(SubscriberSettings, user_id)
        if not sub or sub.following_trader_id != user.id:
            raise HTTPException(404, "not_a_subscriber")
        target_user_id = user_id

    # Pull the latest fills for the target user before computing P&L. The
    # frontend already runs sync-fills for the caller on mount, but when a
    # trader views a *subscriber's* P&L the subscriber's mirror orders may
    # still be at status=submitted with filled_quantity=0 — they'd be
    # excluded from the P&L query and the day would look empty. Sync first
    # so freshly-filled mirrors land on the right day.
    try:
        fills_sync.sync_user_fills(db, target_user_id)
        db.commit()
    except Exception:  # noqa: BLE001
        # Sync failures are non-fatal; we still return whatever P&L exists.
        db.rollback()

    daily = realized_pnl_by_day(db, target_user_id, start=from_, end=to, tz_name=tz)
    return [DailyPnL(day=d, realized_pnl=p, trade_count=n) for d, (p, n) in sorted(daily.items())]


@router.post("/trades/sync-fills")
def sync_fills(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Pull activities from every connected broker and upsert fills locally.
    The Calendar + Trades pages call this on load so realized P&L stays fresh.
    """
    result = fills_sync.sync_user_fills(db, user.id)
    if result["fills_added"] or result["orders_added"]:
        audit.record(
            db,
            actor_user_id=user.id,
            action="fills.synced",
            metadata=result,
            ip_address=client_ip(request),
        )
    db.commit()
    return result
