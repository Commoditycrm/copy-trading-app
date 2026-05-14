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


def _run_fanout_in_background(trader_order_id: uuid.UUID, trader_id: uuid.UUID) -> None:
    """Runs after the response is sent. Opens its own DB session because the
    request-scoped session is closed by the time this fires."""
    with SessionLocal() as db:
        order = db.get(Order, trader_order_id)
        trader = db.get(User, trader_id)
        if order is None or trader is None:
            return
        fan_results = copy_engine.fanout(db, order, trader)
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
) -> Order:
    """Core trader order-placement flow used by both /api/trades (new orders)
    and /api/trades/{id}/close (reverse the position). Builds Order, submits
    to broker, updates status, audits, fires SSE + fan-out background task.

    Returns the persisted Order. Caller commits nothing — this function
    commits before returning.
    """
    if not copy_engine.trader_can_trade(db, trader):
        raise HTTPException(409, "trading_disabled")

    acct = db.get(BrokerAccount, broker_account_id)
    if not acct or acct.user_id != trader.id:
        raise HTTPException(404, "broker_account_not_found")
    if acct.connection_status != "connected":
        raise HTTPException(409, "broker_not_connected")
    creds = decrypt_json(acct.encrypted_credentials)

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
        status=OrderStatus.PENDING,
    )
    db.add(order)
    db.flush()

    adapter = adapter_for(acct, creds)
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
                option_expiry=order.option_expiry,
                option_strike=order.option_strike,
                option_right=order.option_right,
                client_order_id=str(order.id),
            )
        )
    except Exception as exc:  # noqa: BLE001
        order.status = OrderStatus.REJECTED
        order.reject_reason = str(exc)[:480]
        order.closed_at = datetime.now(timezone.utc)
        audit.record(
            db, actor_user_id=trader.id, action="trader.order_rejected_at_broker",
            entity_type="order", entity_id=order.id,
            metadata={"error": str(exc)[:480]}, ip_address=client_ip(request),
        )
        db.commit()
        raise HTTPException(502, f"broker_error: {exc}")

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

    db.commit()
    db.refresh(order)

    events.publish(trader.id, copy_engine._order_event("order.placed", order))
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


@router.post("/trades/{order_id}/close", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def close_trade(
    order_id: uuid.UUID,
    payload: CloseOrderIn,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    trader: User = Depends(require_trader),
) -> Order:
    """Close a filled order by placing a reverse-side order of the same size
    (or smaller, if `quantity` is given). The reverse order is itself a normal
    trader order — it fans out to subscribers, audit-logs, etc.

    Trader-only because closes act like new orders and need to propagate.
    Subscribers wait for the trader to close (their mirror will get a
    matching reverse from the fan-out).
    """
    original = db.execute(
        select(Order).where(Order.id == order_id)
    ).scalar_one_or_none()
    if not original or original.user_id != trader.id:
        raise HTTPException(404, "not_found")
    if original.status != OrderStatus.FILLED:
        raise HTTPException(409, f"not_closeable: original status is {original.status.value}")
    if original.parent_order_id is not None:
        # Shouldn't happen for a trader, but defensive.
        raise HTTPException(409, "cannot_close_mirror")

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
        db, trader, new_payload, original.broker_account_id, background, request
    )

    audit.record(
        db, actor_user_id=trader.id, action="order.closed",
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

    daily = realized_pnl_by_day(db, target_user_id, start=from_, end=to)
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
