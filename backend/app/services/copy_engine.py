"""Copy-trade fan-out (SnapTrade-backed, parallel execution).

When the trader places an order, fan out to every active subscriber's broker
account, scaled by their multiplier. Quantity rounding rule:
  - If broker supports fractional shares: keep raw multiplied quantity (truncated to 6dp).
  - Otherwise: floor to whole shares. If result is 0, skip and audit-log the skip.

Execution model:
  Phase 1 (serial, fast): for each subscriber × broker_account, compute the
                          scaled qty, insert a child Order row in PENDING state.
  Phase 2 (parallel, slow): fire all SnapTrade orders concurrently in a thread
                            pool. Each thread only does the HTTP call, no DB.
  Phase 3 (serial): apply the broker responses back to the child Order rows
                    and audit-log each result. Publish an SSE event per
                    subscriber so their UI updates immediately.

A failure on one subscriber must NOT block the others — handled by per-task
exception capture in Phase 2.
"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers import BrokerOrderRequest, BrokerOrderResult, SnapTradeBrokerAdapter
from app.models.broker_account import BrokerAccount
from app.models.order import Order, OrderStatus
from app.models.settings import SubscriberSettings, TraderSettings
from app.models.user import User, UserRole
from app.services import audit, events, snaptrade as st
from app.services.pnl import today_realized_pnl

MAX_PARALLEL = 32


@dataclass
class FanoutResult:
    subscriber_user_id: uuid.UUID
    broker_account_id: uuid.UUID
    order_id: uuid.UUID | None
    status: str       # "submitted" | "skipped_zero_qty" | "skipped_no_broker" | "skipped_no_snaptrade" | "error"
    detail: str | None = None


@dataclass
class _PendingMirror:
    """Phase-1 output: a child Order row already inserted, plus what the
    SnapTrade call needs."""
    child_order_id: uuid.UUID
    subscriber_user_id: uuid.UUID
    broker_account_id: uuid.UUID
    snaptrade_account_id: str
    user_secret: str
    request: BrokerOrderRequest


def _scale_quantity(trader_qty: Decimal, multiplier: Decimal, fractional: bool) -> Decimal:
    raw = trader_qty * multiplier
    if fractional:
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    return raw.to_integral_value(rounding=ROUND_DOWN)


def trader_can_trade(db: Session, trader: User) -> bool:
    if trader.role != UserRole.TRADER:
        return False
    settings = db.get(TraderSettings, trader.id)
    return bool(settings and settings.trading_enabled)


def fanout(db: Session, trader_order: Order, trader: User) -> list[FanoutResult]:
    """Mirror `trader_order` to all subscribers following this trader.
    Caller commits the session."""
    results: list[FanoutResult] = []
    pending: list[_PendingMirror] = []

    # ── Phase 1: build child orders + skip records ─────────────────────────
    sub_rows = (
        db.execute(
            select(SubscriberSettings).where(
                SubscriberSettings.following_trader_id == trader.id,
                SubscriberSettings.copy_enabled.is_(True),
            )
        )
        .scalars()
        .all()
    )

    for sub_settings in sub_rows:
        sub_user = db.get(User, sub_settings.user_id)
        if not sub_user or not sub_user.encrypted_snaptrade_user_secret:
            results.append(FanoutResult(
                subscriber_user_id=sub_settings.user_id,
                broker_account_id=uuid.UUID(int=0),
                order_id=None,
                status="skipped_no_snaptrade",
            ))
            continue

        # Daily-loss kill switch: if today's realized loss already exceeds the
        # subscriber's limit, flip copy off and skip. We check BEFORE placing
        # the order so we never blow past the limit even by one trade.
        if sub_settings.daily_loss_limit is not None:
            todays_pnl = today_realized_pnl(db, sub_settings.user_id)
            if todays_pnl <= -sub_settings.daily_loss_limit:
                sub_settings.copy_enabled = False
                audit.record(
                    db,
                    actor_user_id=sub_settings.user_id,
                    action="copy.auto_paused_daily_loss",
                    entity_type="subscriber_settings",
                    entity_id=sub_settings.user_id,
                    metadata={
                        "daily_loss_limit": str(sub_settings.daily_loss_limit),
                        "todays_realized_pnl": str(todays_pnl),
                        "trigger_order_id": str(trader_order.id),
                    },
                )
                events.publish(sub_settings.user_id, {
                    "type": "copy.auto_paused",
                    "reason": "daily_loss_limit",
                    "daily_loss_limit": str(sub_settings.daily_loss_limit),
                    "todays_realized_pnl": str(todays_pnl),
                })
                results.append(FanoutResult(
                    subscriber_user_id=sub_settings.user_id,
                    broker_account_id=uuid.UUID(int=0),
                    order_id=None,
                    status="skipped_daily_loss_limit",
                ))
                continue

        sub_secret = st.decrypt_secret(sub_user.encrypted_snaptrade_user_secret)

        sub_accounts = (
            db.execute(
                select(BrokerAccount).where(BrokerAccount.user_id == sub_settings.user_id)
            )
            .scalars()
            .all()
        )
        if not sub_accounts:
            results.append(FanoutResult(
                subscriber_user_id=sub_settings.user_id,
                broker_account_id=uuid.UUID(int=0),
                order_id=None,
                status="skipped_no_broker",
            ))
            continue

        for acct in sub_accounts:
            scaled = _scale_quantity(
                trader_order.quantity, sub_settings.multiplier, acct.supports_fractional
            )
            if scaled <= 0:
                audit.record(
                    db,
                    actor_user_id=sub_settings.user_id,
                    action="copy.skipped_zero_qty",
                    entity_type="order",
                    entity_id=trader_order.id,
                    metadata={
                        "trader_qty": str(trader_order.quantity),
                        "multiplier": str(sub_settings.multiplier),
                        "broker_account_id": str(acct.id),
                    },
                )
                results.append(FanoutResult(
                    subscriber_user_id=sub_settings.user_id,
                    broker_account_id=acct.id,
                    order_id=None,
                    status="skipped_zero_qty",
                ))
                continue

            child = Order(
                user_id=sub_settings.user_id,
                broker_account_id=acct.id,
                parent_order_id=trader_order.id,
                instrument_type=trader_order.instrument_type,
                symbol=trader_order.symbol,
                option_expiry=trader_order.option_expiry,
                option_strike=trader_order.option_strike,
                option_right=trader_order.option_right,
                side=trader_order.side,
                order_type=trader_order.order_type,
                quantity=scaled,
                limit_price=trader_order.limit_price,
                stop_price=trader_order.stop_price,
                status=OrderStatus.PENDING,
            )
            db.add(child)
            db.flush()

            pending.append(_PendingMirror(
                child_order_id=child.id,
                subscriber_user_id=sub_settings.user_id,
                broker_account_id=acct.id,
                snaptrade_account_id=acct.snaptrade_account_id,
                user_secret=sub_secret,
                request=BrokerOrderRequest(
                    instrument_type=child.instrument_type,
                    symbol=child.symbol,
                    side=child.side,
                    order_type=child.order_type,
                    quantity=child.quantity,
                    limit_price=child.limit_price,
                    stop_price=child.stop_price,
                    option_expiry=child.option_expiry,
                    option_strike=child.option_strike,
                    option_right=child.option_right,
                    client_order_id=str(child.id),
                ),
            ))

    # ── Phase 2: fire all broker calls in parallel ─────────────────────────
    def _place(item: _PendingMirror) -> tuple[_PendingMirror, BrokerOrderResult | None, str | None]:
        try:
            adapter = SnapTradeBrokerAdapter(
                app_user_id=item.subscriber_user_id,
                user_secret=item.user_secret,
                snaptrade_account_id=item.snaptrade_account_id,
            )
            return item, adapter.place_order(item.request), None
        except Exception as exc:  # noqa: BLE001
            return item, None, str(exc)[:480]

    if pending:
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(pending))) as pool:
            broker_results = list(pool.map(_place, pending))
    else:
        broker_results = []

    # ── Phase 3: apply results, audit, publish events ──────────────────────
    for item, resp, err in broker_results:
        child = db.get(Order, item.child_order_id)
        if resp is not None:
            child.status = resp.status
            child.broker_order_id = resp.broker_order_id
            child.submitted_at = resp.submitted_at
            child.filled_quantity = resp.filled_quantity
            child.filled_avg_price = resp.filled_avg_price
            audit.record(
                db,
                actor_user_id=item.subscriber_user_id,
                action="copy.submitted",
                entity_type="order",
                entity_id=child.id,
                metadata={
                    "parent_order_id": str(trader_order.id),
                    "broker_order_id": resp.broker_order_id,
                    "scaled_qty": str(child.quantity),
                },
            )
            results.append(FanoutResult(
                subscriber_user_id=item.subscriber_user_id,
                broker_account_id=item.broker_account_id,
                order_id=child.id,
                status="submitted",
            ))
            events.publish(item.subscriber_user_id, _order_event("order.copy_submitted", child))
        else:
            child.status = OrderStatus.REJECTED
            child.reject_reason = err
            child.closed_at = datetime.now(timezone.utc)
            audit.record(
                db,
                actor_user_id=item.subscriber_user_id,
                action="copy.error",
                entity_type="order",
                entity_id=child.id,
                metadata={"parent_order_id": str(trader_order.id), "error": err},
            )
            results.append(FanoutResult(
                subscriber_user_id=item.subscriber_user_id,
                broker_account_id=item.broker_account_id,
                order_id=child.id,
                status="error",
                detail=err[:200] if err else None,
            ))
            events.publish(item.subscriber_user_id, _order_event("order.copy_failed", child))

    return results


def _order_event(event_type: str, order: Order) -> dict[str, Any]:
    """Compact payload — frontend can use it directly to prepend a row."""
    return {
        "type": event_type,
        "order": {
            "id": str(order.id),
            "parent_order_id": str(order.parent_order_id) if order.parent_order_id else None,
            "broker_account_id": str(order.broker_account_id),
            "symbol": order.symbol,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "quantity": str(order.quantity),
            "filled_quantity": str(order.filled_quantity or 0),
            "filled_avg_price": str(order.filled_avg_price) if order.filled_avg_price else None,
            "status": order.status.value,
            "broker_order_id": order.broker_order_id,
            "instrument_type": order.instrument_type.value,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "reject_reason": order.reject_reason,
        },
    }
