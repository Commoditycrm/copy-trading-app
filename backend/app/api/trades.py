import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import ROUND_DOWN

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, case, func, or_, select, true
from sqlalchemy.orm import Session, selectinload

from app.api.deps import client_ip, current_user, require_trader
from app.brokers import BrokerOrderRequest, adapter_for
from app.database import SessionLocal, get_db
from app.models.broker_account import BrokerAccount
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole
from app.schemas.order import (
    BracketUpdateIn,
    CloseOrderIn,
    DailyPnL,
    OrderOut,
    PlaceOrderIn,
    TradeScopeStats,
    TradeStatsOut,
)
from app.services import audit, copy_engine, events, excel_export, fills_sync, trade_filters
from app.services.crypto import decrypt_json
from app.services.order_retry import is_order_conflict_error, live_closeable_quantity
from app.services.pnl import realized_pnl_by_day

router = APIRouter(prefix="/api", tags=["trades"])


def _instrument_label(o: Order) -> str:
    """"AAPL" for stock; "SPXW 500 CALL 2026-12-19" for an option — the same
    shape the Trades page renders, so a reader can match rows to the UI."""
    if o.instrument_type != InstrumentType.OPTION:
        return o.symbol.upper()
    parts = [o.symbol.upper()]
    if o.option_strike is not None:
        parts.append(str(o.option_strike))
    if o.option_right is not None:
        parts.append(o.option_right.value.upper())
    if o.option_expiry is not None:
        parts.append(str(o.option_expiry))
    return " ".join(parts)


def trade_export_columns() -> list[excel_export.Column]:
    """Columns for an order-level export. Values stay native (datetime /
    Decimal) so Excel can sort and filter them — see services/excel_export."""
    C = excel_export.Column
    M, D = "#,##0.00######", "yyyy-mm-dd hh:mm:ss"
    return [
        C("Placed At (UTC)", lambda o: o.submitted_at or o.created_at, 19, D),
        C("Instrument", _instrument_label, 26),
        C("Symbol", lambda o: o.symbol.upper(), 12),
        C("Type", lambda o: o.instrument_type.value, 10),
        C("Side", lambda o: o.side.value.upper(), 8),
        C("Order Type", lambda o: o.order_type.value, 12),
        C("Status", lambda o: o.status.value, 15),
        C("Quantity", lambda o: o.quantity, 11, M),
        C("Filled Qty", lambda o: o.filled_quantity, 11, M),
        C("Limit Price", lambda o: o.limit_price, 13, M),
        C("Stop Price", lambda o: o.stop_price, 13, M),
        C("Avg Fill Price", lambda o: o.filled_avg_price, 14, M),
        # Notional is the number people actually want and the one they'd get
        # wrong by hand: options are per-contract, so 100x the share price.
        C("Filled Notional", _filled_notional, 15, M),
        C("Take Profit", lambda o: o.take_profit_price, 12, M),
        C("Stop Loss", lambda o: o.stop_loss_price, 12, M),
        C("Copied Trade", lambda o: "yes" if o.parent_order_id else "no", 12),
        C("Sent To Subscribers", lambda o: "yes" if o.fanned_out_to_subscribers else "no", 17),
        C("Reject Reason", lambda o: o.reject_reason, 40),
        C("Broker Order ID", lambda o: o.broker_order_id, 26),
        C("Closed At (UTC)", lambda o: o.closed_at, 19, D),
        C("Order ID", lambda o: str(o.id), 36),
    ]


def _filled_notional(o: Order):
    """filled_qty x avg_price, x100 for options (contract multiplier)."""
    if o.filled_avg_price is None or not o.filled_quantity:
        return None
    mult = 100 if o.instrument_type == InstrumentType.OPTION else 1
    return o.filled_quantity * o.filled_avg_price * mult


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
        # Order by the ACTUAL trade time, not our DB-insert time. Orders
        # imported by a fills re-sync (e.g. after reconnecting a broker)
        # all get created_at = now, which would bunch historical trades at
        # the top. submitted_at carries the broker's real timestamp; fall
        # back to created_at for rows that never reached the broker.
        .order_by(func.coalesce(Order.submitted_at, Order.created_at).desc())
        .limit(limit)
    )
    if from_:
        q = q.where(Order.created_at >= datetime.combine(from_, datetime.min.time(), tzinfo=timezone.utc))
    if to:
        q = q.where(Order.created_at < datetime.combine(to, datetime.min.time(), tzinfo=timezone.utc))
    return list(db.execute(q).scalars())


@router.get("/trades/export")
def export_trades(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    # aliased: the URL param stays ?status=, but binding it to `status` here
    # would shadow fastapi's `status` module and turn every HTTPException in
    # this function into an AttributeError.
    status_tab: str = Query(default="all", alias="status", description="Trades-page status tab"),
    search: str | None = Query(default=None, description="Symbol substring"),
    user_id: uuid.UUID | None = Query(
        default=None,
        description="ADMIN ONLY — export this user's orders instead of your own.",
    ),
) -> Response:
    """Orders as .xlsx, honouring the Trades page filters.

    Defaults to the caller's own orders. Admins may pass ?user_id= to export
    someone else's — unlike the admin Performance export, this covers a trader's
    COMPLETE order history, including orders that never fanned out (Just-me
    scope, copy paused, no subscribers).

    Deliberately NOT limited to the UI's window: /trades caps at 1000 rows for
    the table, but an export that silently stopped at 1000 would look complete
    and not be. (ROW_CAP still applies, and is disclosed in the file.)
    """
    # Authorization. Reading another user's trade history is admin-only — this
    # is the whole ballgame for this endpoint, so it's an explicit deny rather
    # than a silent fallback to `user`: a non-admin passing ?user_id= is either
    # probing or hitting a bug, and both deserve a 403 over quietly handing back
    # their own rows as if the filter had applied.
    target = user
    if user_id is not None and user_id != user.id:
        if user.role != UserRole.ADMIN:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin_only")
        target = db.get(User, user_id)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user_not_found")

    q = (
        select(Order)
        .options(selectinload(Order.fills))
        .where(Order.user_id == target.id)
        .order_by(func.coalesce(Order.submitted_at, Order.created_at).desc())
    )
    if from_:
        q = q.where(Order.created_at >= datetime.combine(from_, datetime.min.time(), tzinfo=timezone.utc))
    if to:
        q = q.where(Order.created_at < datetime.combine(to, datetime.min.time(), tzinfo=timezone.utc))
    q = trade_filters.exclude_dead_bracket_legs(q)
    q = trade_filters.apply_status_tab(q, status_tab)
    q = trade_filters.apply_symbol_search(q, search)

    orders = list(db.execute(q).scalars())
    now = datetime.now(timezone.utc)
    truncated = len(orders) > excel_export.ROW_CAP
    orders = orders[:excel_export.ROW_CAP]

    # actor = who clicked, entity = whose data left the building. They differ on
    # an admin export, and that distinction is the point of logging it.
    audit.record(
        db, actor_user_id=user.id,
        action="trades.exported_other" if target.id != user.id else "trades.exported",
        entity_type="user", entity_id=target.id,
        metadata={"rows": len(orders), "status": status_tab, "search": search,
                  "from": str(from_) if from_ else None, "to": str(to) if to else None,
                  "subject_email": target.email if target.id != user.id else None},
    )
    # Commit BEFORE building the file. build_workbook is pure CPU and runs for
    # ~0.8ms/row — 20k rows is ~15s, which is exactly Postgres's
    # idle_in_transaction_session_timeout here. Hold the transaction open
    # through the build and the connection gets killed mid-request, then
    # get_db's teardown blows up on a dead connection (a 500 with no app frames
    # in the traceback, because it dies in the dependency, not the endpoint).
    # expire_on_commit=False keeps the rows we already loaded readable
    # afterwards — the default would expire them and re-query on every single
    # attribute the columns touch.
    db.expire_on_commit = False
    db.commit()

    data = excel_export.build_workbook(
        columns=trade_export_columns(),
        rows=orders,
        sheet_title="Trades",
        # Record the filters IN the file — otherwise a filtered export is
        # indistinguishable from a full dump once it's been emailed around.
        meta=(
            ("Exported (UTC)", now.replace(tzinfo=None)),
            # Whose trades these are — not who clicked. On an admin export those
            # differ, and mislabelling the file is how someone ends up reading
            # one trader's history as another's.
            ("Account", target.email),
            *((("Exported by", user.email),) if target.id != user.id else ()),
            ("Status filter", status_tab),
            ("Symbol search", search or "(none)"),
            ("From", from_ or "(all time)"),
            ("To", to or "(all time)"),
            ("Rows", len(orders)),
            ("Truncated", f"YES — capped at {excel_export.ROW_CAP:,} rows"
             if truncated else "no"),
        ),
    )
    # Name the subject in the file when it isn't the caller — an admin pulling
    # several traders otherwise gets a folder of same-named downloads.
    # Prefer the business/display name over the email: traders here use
    # admin@<their-domain>, so the email's local part slugs to "admin" for all
    # of them — colliding again, and reading like the ADMIN's own trades.
    prefix = "trades"
    if target.id != user.id:
        label = ((target.business_name or "").strip()
                 or (target.display_name or "").strip()
                 or target.email.replace("@", "-at-"))
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        prefix = f"trades-{slug or target.id.hex[:8]}"
    return Response(
        content=data,
        media_type=excel_export.XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{excel_export.filename(prefix, when=now)}"',
        },
    )


# Statuses the UI counts as "working" — must mirror the frontend's
# OPEN_STATUSES (trades/page.tsx). Deliberately excludes RETRY_PENDING.
_WORKING_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)


@router.get("/trades/stats", response_model=TradeStatsOut)
def trades_stats(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
) -> TradeStatsOut:
    """Order-history summary counts computed in the DATABASE via aggregate
    query — so the totals reflect every matching order, not just the
    page the client fetched. Returns both the ``all`` scope and the
    trader's ``mine`` scope in one round-trip (the tab badges need both).

    Computed live (no denormalised counters) so it can never drift out of
    sync with the orders table; respects the same optional from/to filter
    as ``list_trades``.
    """
    # "All Orders" = every order the user placed (copy ON and OFF) — a
    # superset. "My Orders" (trader only) = the subset placed while copy
    # was OFF (fanned_out=False). So a copy-off order shows under BOTH; a
    # copy-on order shows only under All. Non-traders have no tabs → both
    # scopes are all their orders.
    is_trader = user.role == UserRole.TRADER
    all_cond = true()
    mine_cond = Order.fanned_out_to_subscribers.is_(False) if is_trader else true()
    filled_cond = Order.status == OrderStatus.FILLED
    working_cond = Order.status.in_(_WORKING_STATUSES)

    # Notional = filled_qty × filled_avg_price, ×100 for options. Unfilled
    # rows contribute 0 (filled_quantity is 0 / filled_avg_price is NULL).
    mult = case((Order.instrument_type == InstrumentType.OPTION, 100), else_=1)
    notional_expr = (
        func.coalesce(Order.filled_quantity, 0)
        * func.coalesce(Order.filled_avg_price, 0)
        * mult
    )

    row = db.execute(
        select(
            func.count().filter(all_cond).label("all_total"),
            func.count().filter(and_(all_cond, filled_cond)).label("all_filled"),
            func.count().filter(and_(all_cond, working_cond)).label("all_working"),
            func.coalesce(
                func.sum(case((all_cond, notional_expr), else_=0)), 0
            ).label("all_notional"),
            func.count().filter(mine_cond).label("mine_total"),
            func.count().filter(and_(mine_cond, filled_cond)).label("mine_filled"),
            func.count().filter(and_(mine_cond, working_cond)).label("mine_working"),
            func.coalesce(
                func.sum(case((mine_cond, notional_expr), else_=0)), 0
            ).label("mine_notional"),
        )
        .where(Order.user_id == user.id)
        # Mirror the order-history table's visibility rule: a bracket exit leg
        # (TP/SL) only belongs in history once it actually filled (the real
        # close). Resting / cancelled / rejected legs are auto-placed
        # protective orders, not trades the user placed — excluding them keeps
        # the summary counts honest and in lockstep with the rows shown.
        .where(or_(Order.bracket_parent_id.is_(None), filled_cond))
        .where(
            Order.created_at >= datetime.combine(from_, datetime.min.time(), tzinfo=timezone.utc)
            if from_ else True
        )
        .where(
            Order.created_at < datetime.combine(to, datetime.min.time(), tzinfo=timezone.utc)
            if to else True
        )
    ).one()

    return TradeStatsOut(
        all=TradeScopeStats(
            total=row.all_total,
            filled=row.all_filled,
            working=row.all_working,
            notional=row.all_notional,
        ),
        mine=TradeScopeStats(
            total=row.mine_total,
            filled=row.mine_filled,
            working=row.mine_working,
            notional=row.mine_notional,
        ),
    )


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


def _cancel_conflicting_orders(
    db: Session, user: User, acct: BrokerAccount, adapter, close_order: Order,
) -> list[uuid.UUID]:
    """Cancel ALL of this user's still-working orders for the SAME contract —
    both the opposite-side ones (wash trade) and the same-side ones that already
    reserve the position (a second close is 'uncovered' / 'insufficient qty').
    These are exactly the orders blocking the close. Cancels at the broker and
    marks each CANCELED locally. Returns the ids we actually cancelled. The
    close order itself is excluded (it has no broker_order_id yet)."""
    rows = db.execute(
        select(Order).where(
            Order.user_id == user.id,
            Order.broker_account_id == acct.id,
            Order.instrument_type == close_order.instrument_type,
            Order.symbol == close_order.symbol,
            Order.option_expiry.is_not_distinct_from(close_order.option_expiry),
            Order.option_strike.is_not_distinct_from(close_order.option_strike),
            Order.option_right.is_not_distinct_from(close_order.option_right),
            Order.status.in_(_CANCELLABLE_STATUSES),
            Order.broker_order_id.isnot(None),
        )
    ).scalars().all()
    cancelled: list[uuid.UUID] = []
    now = datetime.now(timezone.utc)
    for o in rows:
        try:
            adapter.cancel_order(o.broker_order_id)
        except Exception:  # noqa: BLE001
            import logging  # noqa: PLC0415
            logging.getLogger(__name__).warning(
                "close: failed to cancel conflicting order %s (broker_order=%s)",
                o.id, o.broker_order_id,
            )
            continue
        o.status = OrderStatus.CANCELED
        o.closed_at = now
        cancelled.append(o.id)
    if cancelled:
        db.flush()
    return cancelled


def _place_trader_order(
    db: Session,
    trader: User,
    payload: PlaceOrderIn,
    broker_account_id: uuid.UUID,
    background: BackgroundTasks,
    request: Request,
    skip_fanout: bool = False,
    resolve_wash_trade: bool = False,
) -> Order:
    """Core order-placement flow. Used by /api/trades for trader-originated
    orders (which fan out to subscribers) and by close endpoints. Also reused
    for subscriber-originated closes — in that case we skip the trader
    kill-switch check and don't fan anything out.

    Returns the persisted Order. Caller commits nothing — this function
    commits before returning.
    """
    # ── Concurrency guard (race-free duplicate suppression) ───────────────
    # Two near-simultaneous requests for the SAME order would each pass the
    # dedup SELECT below (the first request's row isn't committed yet) and
    # each place a REAL broker order — the production duplicate we saw. A
    # Postgres transaction-level advisory lock keyed on the order's IDENTITY
    # serializes them: the second request waits until the first commits, then
    # the dedup SELECT sees that order and returns it instead of placing again.
    #
    # The key is the order identity ONLY, so two DIFFERENT orders never block
    # each other — legitimate distinct trades are completely unaffected. The
    # lock auto-releases at transaction end (the commit at the bottom of this
    # function). lock_timeout caps the WAIT so a placement stuck at the broker
    # can't block the identical order indefinitely; on timeout we fall through
    # to the dedup window (which still catches an already-committed duplicate).
    # Postgres-only — on other engines (e.g. SQLite in tests) we skip silently.
    _bind = db.get_bind()
    if getattr(getattr(_bind, "dialect", None), "name", "") == "postgresql":
        import hashlib  # noqa: PLC0415
        from sqlalchemy import text  # noqa: PLC0415
        _identity = "|".join(str(x) for x in (
            trader.id, broker_account_id, payload.instrument_type.value,
            payload.symbol.upper(), payload.side.value, payload.order_type.value,
            payload.quantity, payload.limit_price, payload.stop_price,
            payload.option_expiry, payload.option_strike,
            payload.option_right.value if payload.option_right else None,
        ))
        _lock_key = int.from_bytes(
            hashlib.sha256(_identity.encode()).digest()[:8], "big", signed=True
        )
        # Acquire inside a SAVEPOINT so a lock-wait timeout rolls back ONLY the
        # lock attempt — never any work a caller (e.g. a position-close path)
        # already did in this transaction. If acquired, the advisory lock is
        # held at the top-level tx and released by this function's commit.
        try:
            with db.begin_nested():
                db.execute(text("SET LOCAL lock_timeout = '12s'"))
                db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key})
        except Exception:  # noqa: BLE001 — lock-wait timeout / engine hiccup
            import logging  # noqa: PLC0415
            logging.getLogger(__name__).warning(
                "trades: advisory lock not acquired (user=%s symbol=%s); "
                "relying on the dedup window", trader.id, payload.symbol,
            )

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
        # Close paths pass resolve_wash_trade=True — mark the order as a close so
        # the flag propagates to the subscriber mirror (which uses it to gate the
        # close-side quantity clamp and the copy-engine conflict-resolve retry).
        is_closing=resolve_wash_trade,
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
    # Tell the broker listener this order is ours BEFORE we place it. The
    # broker echoes order.id back as client_order_id on the trade_updates
    # stream; the listener checks this marker and won't create a duplicate
    # parent + second fanout if its WS event beats our commit below.
    from app.services import order_intent  # noqa: PLC0415
    order_intent.mark_app_originated(order.id)

    broker_req = BrokerOrderRequest(
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

    _broker_t0 = time.perf_counter()
    # Auto-resolve wash-trade rejections on CLOSE orders: if the broker rejects
    # because an opposite-side order for this contract is still working, cancel
    # that order and retry — so the user doesn't have to cancel it manually and
    # re-close. `resolve_wash_trade` gates this to close paths; we never
    # auto-cancel resting orders to force an ENTRY through.
    _cancelled_conflicts = False
    _reclamped_to_live = False
    _wash_retries = 0
    _rounded_whole = False
    while True:
        try:
            result = adapter.place_order(broker_req)
            break
        except Exception as exc:  # noqa: BLE001
            # Non-fractionable asset + fractional qty (e.g. closing 25% of 10 =
            # 2.5 shares of a stock Alpaca won't split) → round the quantity DOWN
            # to whole shares and retry once. Applies to any order; rounding down
            # never exceeds the intended size / held position.
            if (
                not _rounded_whole
                and "not fractionable" in str(exc).lower()
                and order.quantity != order.quantity.to_integral_value(rounding=ROUND_DOWN)
            ):
                _rounded_whole = True
                whole = order.quantity.to_integral_value(rounding=ROUND_DOWN)
                if whole > 0:
                    audit.record(
                        db, actor_user_id=trader.id, action="order.rounded_to_whole",
                        entity_type="order", entity_id=order.id,
                        metadata={"from_qty": str(order.quantity), "to_qty": str(whole),
                                  "symbol": order.symbol, "reason": "not_fractionable"},
                        ip_address=client_ip(request),
                    )
                    order.quantity = whole
                    broker_req = replace(broker_req, quantity=whole)
                    continue
            if resolve_wash_trade and is_order_conflict_error(exc):
                # Re-clamp to the broker's LIVE held quantity (source of truth).
                # Fixes a close rejected for exceeding holdings when our DB
                # position was stale (e.g. a prior partial fill hadn't synced).
                # Only ever SHRINKS the order, so it can never oversell.
                if not _reclamped_to_live:
                    _reclamped_to_live = True
                    live = live_closeable_quantity(adapter, broker_req)
                    if live is not None and 0 < live < broker_req.quantity:
                        audit.record(
                            db, actor_user_id=trader.id,
                            action="close.reclamped_to_live_qty",
                            entity_type="order", entity_id=order.id,
                            metadata={"from_qty": str(broker_req.quantity),
                                      "to_qty": str(live), "symbol": order.symbol,
                                      "reason": str(exc)[:200]},
                            ip_address=client_ip(request),
                        )
                        order.quantity = live
                        broker_req = replace(broker_req, quantity=live)
                        _wash_retries = max(_wash_retries, 3)
                if not _cancelled_conflicts:
                    _cancelled_conflicts = True
                    cancelled = _cancel_conflicting_orders(db, trader, acct, adapter, order)
                    if cancelled:
                        audit.record(
                            db, actor_user_id=trader.id,
                            action="close.cancelled_conflicting_order",
                            entity_type="order", entity_id=order.id,
                            metadata={
                                "cancelled_order_ids": [str(x) for x in cancelled],
                                "symbol": order.symbol,
                                "reason": str(exc)[:200],
                            },
                            ip_address=client_ip(request),
                        )
                        # Give the broker a moment to release the reservation
                        # before we retry (a few short attempts).
                        _wash_retries = 3
                if _wash_retries > 0:
                    _wash_retries -= 1
                    time.sleep(0.5)
                    continue
            order.broker_call_ms = int((time.perf_counter() - _broker_t0) * 1000)
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)[:480]
            order.closed_at = datetime.now(timezone.utc)
            audit.record(
                db, actor_user_id=trader.id, action="trader.order_rejected_at_broker",
                entity_type="order", entity_id=order.id,
                metadata={"error": str(exc)[:480]}, ip_address=client_ip(request),
            )
            # Notify the trader too — in-app + SMS for opted-in traders. Always
            # fired (not gated on will_fanout): the trader wants to know their
            # own order was rejected regardless of copy scope.
            try:
                from app.services import notifications as notif_svc  # noqa: PLC0415
                notif_svc.create_notification(
                    db,
                    user_id=trader.id,
                    type="order.rejected",
                    message=(
                        f"Your {order.side.value.upper()} {order.symbol} order was "
                        f"rejected: {str(exc)[:180]}"
                    ),
                    metadata={
                        "order_id": str(order.id),
                        "symbol": order.symbol,
                        "side": order.side.value,
                        "reason": str(exc)[:300],
                    },
                )
            except Exception:  # noqa: BLE001
                import logging  # noqa: PLC0415
                logging.getLogger(__name__).exception(
                    "trades: trader rejection notification failed for order %s", order.id
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

    # Resolve which broker account to cancel through. An order placed on a
    # PREVIOUS connection becomes "orphaned" once that broker is deleted /
    # reconnected: ON DELETE SET NULL clears broker_account_id. Rather than
    # stranding it as un-cancellable, route the cancel through the user's
    # currently-connected account(s) and re-adopt the order onto whichever
    # one the broker accepts the cancel from.
    acct = db.get(BrokerAccount, order.broker_account_id) if order.broker_account_id else None
    reattached = False

    if order.broker_order_id and acct is not None:
        # Normal path — cancel at the broker on the order's own account.
        # If the broker rejects (e.g. already filled), surface the error
        # but DON'T mutate local state — DB stays accurate.
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

    elif order.broker_order_id and acct is None:
        # Orphaned: try the user's connected accounts. cancel_order with a
        # broker_order_id the account doesn't own fails cleanly (id not
        # found there), so trying each is safe.
        candidates = list(db.execute(
            select(BrokerAccount).where(
                BrokerAccount.user_id == user.id,
                BrokerAccount.connection_status == "connected",
            )
        ).scalars())
        last_err: Exception | None = None
        for cand in candidates:
            try:
                creds = decrypt_json(cand.encrypted_credentials)
                adapter_for(cand, creds).cancel_order(order.broker_order_id)
                acct = cand
                order.broker_account_id = cand.id   # re-adopt onto the live account
                reattached = True
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        if acct is None and candidates:
            # A broker is connected but none could cancel it (wrong broker,
            # or already terminal upstream). Surface the broker error.
            audit.record(
                db, actor_user_id=user.id, action="order.cancel_failed",
                entity_type="order", entity_id=order.id,
                metadata={"error": str(last_err)[:480], "orphaned": True},
                ip_address=client_ip(request),
            )
            db.commit()
            raise HTTPException(502, f"broker_error: {last_err}")
        # No connected broker at all → unreachable; fall through to a local
        # cancel so the order doesn't linger forever as a ghost open order.

    # else: order never reached a broker (no broker_order_id) → local cancel.

    order.status = OrderStatus.CANCELED
    order.closed_at = datetime.now(timezone.utc)
    audit.record(
        db, actor_user_id=user.id, action="order.cancelled",
        entity_type="order", entity_id=order.id,
        metadata={
            "broker_order_id": order.broker_order_id,
            "reattached_account": str(acct.id) if reattached else None,
            "local_only": acct is None,
        },
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
        db, user, new_payload, original.broker_account_id, background, request,
        resolve_wash_trade=True,
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


@router.patch("/trades/{order_id}/bracket", response_model=OrderOut)
def update_bracket(
    order_id: uuid.UUID,
    payload: BracketUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> Order:
    """Modify the TP / SL legs on an entry order's bracket.

    Behaviour by state:
      * Entry not yet filled (pending / submitted / accepted / partially_filled)
        → just update parent.take_profit_price / stop_loss_price. The
        bracket_emulator places exit legs at the new prices when the
        entry eventually fills.
      * Entry filled, exit legs handled by emulator (i.e. NOT Alpaca-native
        stocks bracket) → cancel any alive exit leg the caller is replacing,
        update parent prices, then call emulate_bracket_exits to place fresh
        legs. emulate_bracket_exits' relaxed idempotency check only blocks
        on alive/filled siblings, so a canceled leg won't stop a re-place.
      * Entry filled, Alpaca-native bracket (stocks on AlpacaAdapter direct)
        → 501. We don't have Alpaca's child-leg broker_order_ids stored,
        and the project's broker layer is SnapTrade (memory:
        snaptrade_decision); routing live-Alpaca-bracket modify through
        Alpaca's PATCH /v2/orders/{id} can be a follow-up if needed.
      * Entry terminal (canceled / rejected) → 409.
      * Any exit leg already FILLED (position partially closed via TP/SL)
        → 409. Modifying around a partial exit is a confusing edge case
        for v1; trader can place a fresh bracketed order if needed.

    The caller may pass either field, both, or null to clear that leg.
    Pre-fill clearing just nulls the parent column; post-fill clearing
    cancels the corresponding live exit leg without re-placing.
    """
    entry = db.execute(
        select(Order).where(Order.id == order_id)
    ).scalar_one_or_none()
    if not entry or entry.user_id != user.id:
        raise HTTPException(404, "not_found")
    if entry.bracket_leg is not None:
        raise HTTPException(409, "cannot_modify_bracket_leg: pick the entry order, not the exit leg")
    if entry.broker_account_id is None:
        raise HTTPException(409, "broker_disconnected: reconnect the broker first")

    # Lock entry row so two concurrent PATCHes can't race on cancel/re-place.
    db.execute(select(Order).where(Order.id == entry.id).with_for_update())

    # ── Reference price for geometry checks ──────────────────────────────
    # Use the SAME anchor the order was originally bracketed against —
    # ``limit_price`` for any order that had one (which is what
    # PlaceOrderIn's bracket validator uses too) and the actual fill
    # price as the fallback for market entries that carry a bracket.
    # The frontend uses the same precedence to display the percentage,
    # so what the user types as "2%" round-trips to the exact price the
    # backend then validates against. Using filled_avg_price here while
    # the frontend used limit_price as its display anchor caused the
    # "buy_sl_must_be_below_entry" rejections on edits — the two ends
    # were anchoring on different numbers and could disagree by the
    # fill-slippage amount.
    is_filled = entry.status == OrderStatus.FILLED
    ref_price = entry.limit_price or entry.filled_avg_price

    # Effective values after applying the patch. For fields the caller
    # omitted, keep the current value. For explicit-null, clear. For a
    # number, use it.
    new_tp = payload.take_profit_price if payload.tp_present else entry.take_profit_price
    new_sl = payload.stop_loss_price if payload.sl_present else entry.stop_loss_price

    # Directional geometry: buy → sl < ref < tp; sell → tp < ref < sl.
    # Only enforced when ref_price is known AND both legs are set (one-leg
    # brackets have no directional constraint to check beyond ref-side).
    if ref_price is not None:
        if new_tp is not None:
            if entry.side == OrderSide.BUY and new_tp <= ref_price:
                raise HTTPException(422, "buy_tp_must_be_above_entry")
            if entry.side == OrderSide.SELL and new_tp >= ref_price:
                raise HTTPException(422, "sell_tp_must_be_below_entry")
        if new_sl is not None:
            if entry.side == OrderSide.BUY and new_sl >= ref_price:
                raise HTTPException(422, "buy_sl_must_be_below_entry")
            if entry.side == OrderSide.SELL and new_sl <= ref_price:
                raise HTTPException(422, "sell_sl_must_be_above_entry")

    old_tp = entry.take_profit_price
    old_sl = entry.stop_loss_price

    # ── Pre-fill path: DB-only update ────────────────────────────────────
    if not is_filled:
        ALIVE_FOR_MODIFY = (
            OrderStatus.PENDING, OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED,
        )
        if entry.status not in ALIVE_FOR_MODIFY:
            raise HTTPException(409, f"not_modifiable: entry status is {entry.status.value}")
        if payload.tp_present:
            entry.take_profit_price = payload.take_profit_price
        if payload.sl_present:
            entry.stop_loss_price = payload.stop_loss_price
        audit.record(
            db, actor_user_id=user.id, action="bracket.updated_pre_fill",
            entity_type="order", entity_id=entry.id,
            metadata={
                "old_tp": str(old_tp) if old_tp is not None else None,
                "new_tp": str(entry.take_profit_price) if entry.take_profit_price is not None else None,
                "old_sl": str(old_sl) if old_sl is not None else None,
                "new_sl": str(entry.stop_loss_price) if entry.stop_loss_price is not None else None,
            },
            ip_address=client_ip(request),
        )
        db.commit()
        db.refresh(entry)
        return entry

    # ── Post-fill path: cancel live exit legs + re-place ─────────────────
    from app.brokers.alpaca import AlpacaAdapter  # noqa: PLC0415
    from app.services.bracket_emulator import emulate_bracket_exits  # noqa: PLC0415
    from app.models.broker_account import BrokerName  # noqa: PLC0415
    from app.models.order import InstrumentType  # noqa: PLC0415

    # Alpaca-native bracket = Alpaca-direct stocks. We don't track those
    # children locally, so a modify needs Alpaca's PATCH /v2/orders/{id}
    # against the child legs — out of scope for v1.
    acct = db.get(BrokerAccount, entry.broker_account_id)
    uses_native = (
        acct is not None
        and acct.broker == BrokerName.ALPACA
        and entry.instrument_type != InstrumentType.OPTION
    )
    if uses_native:
        raise HTTPException(501, "alpaca_native_bracket_modify_not_supported")

    # Inspect the current exit legs. We block modify if any have ALREADY
    # FILLED — partial exit is a confusing state to modify around.
    legs = db.execute(
        select(Order).where(Order.bracket_parent_id == entry.id)
    ).scalars().all()
    if any(leg.status == OrderStatus.FILLED for leg in legs):
        raise HTTPException(409, "position_partially_closed: a bracket leg already filled")

    ALIVE = (
        OrderStatus.PENDING, OrderStatus.SUBMITTED,
        OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED,
    )
    alive_legs = [leg for leg in legs if leg.status in ALIVE]

    # Cancel each alive leg whose price is changing (or being cleared).
    # If a side wasn't touched in this patch, leave that leg alone.
    creds = decrypt_json(acct.encrypted_credentials)
    adapter = adapter_for(acct, creds)
    for leg in alive_legs:
        if leg.bracket_leg == "tp" and not payload.tp_present:
            continue
        if leg.bracket_leg == "sl" and not payload.sl_present:
            continue
        if leg.broker_order_id:
            try:
                adapter.cancel_order(leg.broker_order_id)
            except Exception as exc:  # noqa: BLE001
                # Broker may have already terminalized it between our last
                # poll and this call; we still mark it locally and proceed.
                audit.record(
                    db, actor_user_id=user.id, action="bracket.leg_cancel_failed",
                    entity_type="order", entity_id=leg.id,
                    metadata={
                        "entry_order_id": str(entry.id),
                        "leg": leg.bracket_leg,
                        "broker_order_id": leg.broker_order_id,
                        "error": str(exc)[:300],
                    },
                )
        leg.status = OrderStatus.CANCELED
        leg.closed_at = datetime.now(timezone.utc)

    # Apply the new prices to the parent.
    if payload.tp_present:
        entry.take_profit_price = payload.take_profit_price
    if payload.sl_present:
        entry.stop_loss_price = payload.stop_loss_price

    # Flush so emulate_bracket_exits sees the canceled-leg statuses
    # (its idempotency guard ignores canceled/rejected legs).
    db.flush()
    placed = emulate_bracket_exits(db, entry)

    audit.record(
        db, actor_user_id=user.id, action="bracket.updated_post_fill",
        entity_type="order", entity_id=entry.id,
        metadata={
            "old_tp": str(old_tp) if old_tp is not None else None,
            "new_tp": str(entry.take_profit_price) if entry.take_profit_price is not None else None,
            "old_sl": str(old_sl) if old_sl is not None else None,
            "new_sl": str(entry.stop_loss_price) if entry.stop_loss_price is not None else None,
            "legs_cancelled": [str(leg.id) for leg in alive_legs],
            "legs_placed": [str(leg.id) for leg in placed],
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(entry)
    return entry


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

    # Subscribers' Webull/SnapTrade orders get re-recorded by the listener as
    # duplicate standalone rows; count only their copy-mirror orders so realized
    # P&L isn't double-counted. See realized_pnl_by_day(mirrors_only=...).
    target = db.get(User, target_user_id)
    mirrors_only = target is not None and target.role == UserRole.SUBSCRIBER
    daily = realized_pnl_by_day(
        db, target_user_id, start=from_, end=to, tz_name=tz, mirrors_only=mirrors_only
    )
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
