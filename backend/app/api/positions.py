"""Open positions — currently held shares/contracts across the trader's broker
accounts.

GET  /api/positions               aggregates positions across every connected
                                  broker account for the caller.
POST /api/positions/{symbol}/close
                                  places a reverse-side order to flatten the
                                  named position. Routes through the same
                                  _place_trader_order flow as a regular order
                                  so it audits, fans out to subscribers, and
                                  publishes an SSE event.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import current_user, require_trader
from app.api.trades import _place_trader_order
from collections.abc import Callable
from decimal import Decimal

from app.brokers import adapter_for
from app.brokers.base import BrokerPosition
from app.database import get_db
from app.models.broker_account import BrokerAccount
from datetime import date
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole
from app.schemas.order import OrderOut, PlaceOrderIn
from app.schemas.position import ClosePositionIn, PositionOut
from app.models.sell_all_snapshot import SellAllSnapshot
from app.services import trailing_stop_close
from app.services.crypto import decrypt_json

log = logging.getLogger(__name__)

# How long any single broker call is allowed to run inside a bulk-exit
# fan-out before we abandon that subscriber and move on. 60s covers
# SnapTrade-Alpaca during throttling (cancels can take 30-60s end to
# end). Past 60s and the broker is almost certainly genuinely hung;
# letting the request finish with a partial result is better UX than
# blocking the user indefinitely. The listener still reconciles any
# cancel that the broker eventually accepts after our timeout — the
# error row makes that clear to the caller.
_BULK_EXIT_BROKER_TIMEOUT_S = 60.0

# Cap on parallel broker calls inside the background close-positions
# sweep. 4 keeps us inside SnapTrade's 250 req/min platform quota
# even when bulk-cancel-subscribers is running too — both endpoints
# share the SnapTrade rate-limit pool.
_BULK_EXIT_CONCURRENCY = 4


class _MinimalRequestShim:
    """Duck-typed stand-in for FastAPI's Request used when we need to
    call ``_place_trader_order`` from a worker thread (no real request
    in scope). ``_place_trader_order`` only touches ``request`` via
    ``client_ip(request)`` which reads ``headers.get('x-forwarded-for')``
    and ``client.host``. We supply just enough of each."""

    def __init__(self, client_ip_str: str | None) -> None:
        self.headers = {}
        if client_ip_str:
            class _ClientStub:
                host = client_ip_str
            self.client = _ClientStub()
        else:
            self.client = None

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.get("", response_model=list[PositionOut])
def list_positions(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[PositionOut]:
    """Return positions across every connected broker account for the caller.

    A position appears once per (broker_account, symbol). Disconnected accounts
    are skipped silently. Per-account broker failures are skipped silently too —
    we don't want one flaky broker to break the whole list.
    """
    accts = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().all()

    out: list[PositionOut] = []
    for acct in accts:
        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
            for p in adapter.get_positions():
                out.append(PositionOut(
                    broker_account_id=acct.id,
                    broker_symbol=p.broker_symbol,
                    symbol=p.symbol,
                    instrument_type=p.instrument_type,
                    quantity=p.quantity,
                    avg_entry_price=p.avg_entry_price,
                    current_price=p.current_price,
                    market_value=p.market_value,
                    unrealized_pnl=p.unrealized_pnl,
                    cost_basis=p.cost_basis,
                    option_expiry=p.option_expiry,
                    option_strike=p.option_strike,
                    option_right=p.option_right,
                ))
        except Exception:  # noqa: BLE001
            # Best-effort: one broker's outage shouldn't blank the whole table.
            continue
    return out


@router.get("/today-realized")
def today_realized(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, float]:
    """Today's realized P&L for the caller, in their market timezone.

    Used by the positions-page summary strip. Subscribers get mirror
    de-duplication (their real trades ARE the copy mirrors — the SnapTrade
    listener re-records those as duplicate standalone rows), so we call
    ``realized_pnl_by_day`` directly with ``mirrors_only`` rather than the
    plain ``today_realized_pnl`` helper which doesn't dedup.
    """
    from datetime import datetime

    from app.services.pnl import _tz_or_market, realized_pnl_by_day

    tz = _tz_or_market(None)
    today = datetime.now(tz).date()
    daily = realized_pnl_by_day(
        db, user.id, start=today, end=today,
        mirrors_only=(user.role == UserRole.SUBSCRIBER),
    )
    pnl, _ = daily.get(today, (Decimal(0), 0))
    return {"realized_pnl": float(pnl)}


@router.post("/close-all")
def close_all_positions(
    request: Request,
    background: BackgroundTasks,
    include_subscribers: bool = Query(
        default=True,
        description="When false, suppress the trader→subscriber fanout. Only the caller's own positions are closed. No-op semantic when caller is a subscriber.",
    ),
    trail_percent: Decimal | None = Query(
        default=None, gt=0, le=100,
        description="Sell-All trailing-stop variation. When set, stock positions on brokers that support trailing stops are closed with a TRAILING_STOP at this trail %; options and unsupported brokers fall back to the normal market/limit close.",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Flatten every open position across the caller's connected broker
    accounts by placing a market reverse order for each. For traders this
    normally fans out to subscribers; pass `include_subscribers=false` to
    close only the trader's own positions without propagating. Per-position
    failures don't abort the rest — we return a per-position result list.

    ``trail_percent`` enables the trailing-stop variation of Sell-All (see the
    per-position logic below and services.trailing_stop_close).
    """
    accts = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().all()

    closed: list[dict] = []
    failed: list[dict] = []
    # Snapshot the positions we're about to flatten so the user can Re-Enter
    # them later (Sell-All snapshot + re-entry). Saved once, after the sweep.
    snapshot_positions: list[dict] = []
    skip_fanout = not include_subscribers

    for acct in accts:
        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
            positions = adapter.get_positions()
        except Exception as exc:  # noqa: BLE001
            failed.append({
                "broker_account_id": str(acct.id),
                "symbol": None,
                "error": f"could not list positions: {exc}"[:300],
            })
            continue

        for pos in positions:
            if pos.quantity == 0:
                continue
            reverse_side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
            qty = abs(pos.quantity)
            # Record what we held (signed qty + exit price) for Re-Enter.
            snapshot_positions.append({
                "symbol": pos.symbol,
                "instrument_type": pos.instrument_type.value,
                "quantity": str(pos.quantity),
                "price": str(pos.current_price) if pos.current_price is not None else None,
                "option_expiry": pos.option_expiry.isoformat() if pos.option_expiry else None,
                "option_strike": str(pos.option_strike) if pos.option_strike is not None else None,
                "option_right": pos.option_right.value if pos.option_right else None,
            })
            # Stocks close at MARKET; OPTIONS always as a LIMIT — both brokers
            # refuse option market orders (Alpaca always; Webull on
            # limited-liquidity contracts). See _option_close_limit.
            close_type = OrderType.MARKET
            close_limit: Decimal | None = None
            close_trail: Decimal | None = None
            close_method = "market"
            if (
                trail_percent is not None
                and trailing_stop_close.trailing_stop_supported(adapter, pos)
            ):
                # Sell-All trailing-stop variation, where the broker supports it.
                close_type = OrderType.TRAILING_STOP
                close_trail = trail_percent
                close_method = "trailing_stop"
            elif pos.instrument_type == InstrumentType.OPTION:
                close_type = OrderType.LIMIT
                close_limit = _option_close_limit(adapter, pos, reverse_side)
                close_method = "limit"
            payload = PlaceOrderIn(
                instrument_type=pos.instrument_type,
                symbol=pos.symbol,
                side=reverse_side,
                order_type=close_type,
                quantity=qty,
                limit_price=close_limit,
                stop_price=None,
                trail_percent=close_trail,
                option_expiry=pos.option_expiry if pos.instrument_type == InstrumentType.OPTION else None,
                option_strike=pos.option_strike if pos.instrument_type == InstrumentType.OPTION else None,
                option_right=pos.option_right if pos.instrument_type == InstrumentType.OPTION else None,
            )
            try:
                order = _place_trader_order(
                    db, user, payload, acct.id, background, request,
                    skip_fanout=skip_fanout, resolve_wash_trade=True,
                )
                closed.append({
                    "broker_account_id": str(acct.id),
                    "symbol": pos.symbol,
                    "qty": str(qty),
                    "side": reverse_side.value,
                    "order_id": str(order.id),
                    "method": close_method,
                })
            except Exception as exc:  # noqa: BLE001
                failed.append({
                    "broker_account_id": str(acct.id),
                    "symbol": pos.symbol,
                    "error": str(exc)[:300],
                })

    snapshot_id = None
    if snapshot_positions:
        # One active snapshot per user: this Sell-All supersedes any prior one.
        db.execute(
            update(SellAllSnapshot)
            .where(SellAllSnapshot.user_id == user.id, SellAllSnapshot.active.is_(True))
            .values(active=False)
        )
        snap = SellAllSnapshot(user_id=user.id, positions=snapshot_positions)  # active defaults True
        db.add(snap)
        db.commit()
        snapshot_id = str(snap.id)

    return {
        "closed": closed, "failed": failed,
        "closed_count": len(closed), "failed_count": len(failed),
        "snapshot_id": snapshot_id, "snapshot_count": len(snapshot_positions),
    }


# A re-entry order that's still live (don't re-place — avoids double-buying).
_REENTRY_WORKING = {
    OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED, OrderStatus.RETRY_PENDING,
}


def _reentry_info(db: Session, item: dict) -> "tuple[str, Decimal | None]":
    """(status, reentry_price) for a snapshot item.
      status: 'filled' / 'working' / 'pending' (see below).
      reentry_price: what we re-entered at — the FILL price when filled, the
                     resting LIMIT price when working, else None.
    'filled'  — the buy-back filled; the position is back.
    'working' — a buy-back is resting (waiting to fill).
    'pending' — never re-entered, or the last attempt canceled/expired/rejected —
                so it still NEEDS a (re-)entry (only 'pending' items get placed)."""
    oid = item.get("reentry_order_id")
    if not oid:
        return "pending", None
    o = db.get(Order, uuid.UUID(oid))
    if o is None:
        return "pending", None
    if o.status == OrderStatus.FILLED:
        return "filled", o.filled_avg_price
    if o.status in _REENTRY_WORKING:
        return "working", o.limit_price   # resting at this limit (None for market)
    return "pending", None  # canceled / expired / rejected → re-enter allowed again


def _reentry_status(db: Session, item: dict) -> str:
    """Just the status (see _reentry_info)."""
    return _reentry_info(db, item)[0]


@router.get("/snapshots/latest")
def latest_sell_all_snapshot(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """The user's ACTIVE Sell-All snapshot (the current one — superseded on each
    new Sell-All), each position annotated with its live re-entry status
    (filled / working / pending) plus a summary count. Null if none active."""
    snap = db.execute(
        select(SellAllSnapshot)
        .where(SellAllSnapshot.user_id == user.id, SellAllSnapshot.active.is_(True))
        .order_by(SellAllSnapshot.created_at.desc())
        .limit(1)
    ).scalars().first()
    if not snap:
        return {"snapshot": None}
    positions = []
    for p in snap.positions:
        st, reentry_price = _reentry_info(db, p)
        positions.append({
            "symbol": p["symbol"],
            "instrument_type": p["instrument_type"],
            "quantity": p["quantity"],
            "price": p.get("price"),                                  # exit price
            "reentry_price": str(reentry_price) if reentry_price is not None else None,
            "option_expiry": p.get("option_expiry"),
            "option_strike": p.get("option_strike"),
            "option_right": p.get("option_right"),
            "reentry_status": st,
        })
    summary = {
        "total": len(positions),
        "filled": sum(1 for x in positions if x["reentry_status"] == "filled"),
        "working": sum(1 for x in positions if x["reentry_status"] == "working"),
        "pending": sum(1 for x in positions if x["reentry_status"] == "pending"),
    }
    return {"snapshot": {
        "id": str(snap.id),
        "created_at": snap.created_at.isoformat(),
        "positions": positions,
        "summary": summary,
    }}


@router.post("/re-enter")
def re_enter_from_snapshot(
    request: Request,
    background: BackgroundTasks,
    discount_percent: Decimal | None = Query(
        default=None, ge=0, le=100,
        description="Re-buy each position this % BELOW its exit price (a resting LIMIT). Omit / 0 = market buy now.",
    ),
    snapshot_id: uuid.UUID | None = Query(default=None, description="Which snapshot; omit for the latest."),
    symbol: str | None = Query(
        default=None,
        description="Re-enter ONLY this symbol (per-order re-entry). Omit to re-enter every pending position.",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Re-open the positions from a Sell-All snapshot, FILL-AWARE: only items not
    already back (or with a resting order) get a new buy. Long positions re-buy,
    short positions re-sell, same quantities. With ``discount_percent`` a stock
    re-buy rests as a LIMIT that % below the exit price ('buy the dip'). Pass
    ``symbol`` to re-enter a SINGLE order. Safe to click repeatedly — filled/
    working items are skipped, so no double-buying."""
    q = select(SellAllSnapshot).where(SellAllSnapshot.user_id == user.id)
    if snapshot_id:
        q = q.where(SellAllSnapshot.id == snapshot_id)
    else:
        # Default to the ACTIVE snapshot (the current one).
        q = q.where(SellAllSnapshot.active.is_(True)).order_by(SellAllSnapshot.created_at.desc())
    snap = db.execute(q.limit(1)).scalars().first()
    if not snap or not snap.positions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no_snapshot")

    acct = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().first()
    if acct is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="no_connected_broker")

    disc = Decimal(discount_percent) if discount_percent is not None else Decimal(0)
    placed: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    new_positions: list[dict] = []
    for p in snap.positions:
        # Per-order re-entry: leave every other position untouched.
        if symbol is not None and p["symbol"] != symbol:
            new_positions.append(p)
            continue
        st = _reentry_status(db, p)
        if st in ("filled", "working"):
            # Already back, or a buy-back is resting — don't place again.
            skipped.append({"symbol": p["symbol"], "reason": st})
            new_positions.append(p)
            continue
        try:
            qty = Decimal(p["quantity"])
            side = OrderSide.BUY if qty > 0 else OrderSide.SELL
            it = InstrumentType(p["instrument_type"])
            price = Decimal(p["price"]) if p.get("price") else None
            # Discount only applies to a stock RE-BUY with a known exit price.
            use_limit = disc > 0 and price is not None and side == OrderSide.BUY and it == InstrumentType.STOCK
            limit_price = (price * (Decimal(1) - disc / Decimal(100))).quantize(Decimal("0.01")) if use_limit else None
            payload = PlaceOrderIn(
                instrument_type=it,
                symbol=p["symbol"],
                side=side,
                order_type=OrderType.LIMIT if use_limit else OrderType.MARKET,
                quantity=abs(qty),
                limit_price=limit_price,
                option_expiry=date.fromisoformat(p["option_expiry"]) if p.get("option_expiry") else None,
                option_strike=Decimal(p["option_strike"]) if p.get("option_strike") else None,
                option_right=OptionRight(p["option_right"]) if p.get("option_right") else None,
            )
            order = _place_trader_order(
                db, user, payload, acct.id, background, request,
                skip_fanout=True, resolve_wash_trade=False,
            )
            placed.append({
                "symbol": p["symbol"], "side": side.value, "qty": str(abs(qty)),
                "order_type": payload.order_type.value,
                "limit_price": str(limit_price) if limit_price is not None else None,
                "order_id": str(order.id),
            })
            # Link the buy-back to this item so a later click sees it as
            # working/filled and won't place a duplicate.
            new_positions.append({**p, "reentry_order_id": str(order.id)})
        except Exception as exc:  # noqa: BLE001
            failed.append({"symbol": p.get("symbol"), "error": str(exc)[:300]})
            new_positions.append(p)  # stays 'pending' — retriable next click

    # Reassign (not mutate) so SQLAlchemy persists the updated JSONB.
    snap.positions = new_positions
    db.commit()

    return {
        "placed": placed, "skipped": skipped, "failed": failed,
        "placed_count": len(placed), "skipped_count": len(skipped), "failed_count": len(failed),
        "discount_percent": str(discount_percent) if discount_percent is not None else None,
        "snapshot_id": str(snap.id),
    }


@router.post("/close-all-subscribers")
async def close_all_subscribers_positions(
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> dict:
    """Trader-only: flatten every open position across EVERY subscriber
    following this trader, by placing a market reverse order on each
    subscriber's OWN account. The trader's own positions are NOT touched.

    Returns IMMEDIATELY with a queued count — the actual broker work
    runs in the background. Symmetric to bulk-cancel-subscribers:
    snapshot here, spawn an asyncio.create_task that fans out across
    ``_BULK_EXIT_CONCURRENCY`` workers with per-call
    ``_BULK_EXIT_BROKER_TIMEOUT_S``. Each close publishes an
    ``order.placed`` SSE event so the relevant subscriber's UI
    refreshes on its own.
    """
    sub_ids = list(db.execute(
        select(SubscriberSettings.user_id).where(
            SubscriberSettings.following_trader_id == user.id
        )
    ).scalars())
    if not sub_ids:
        return {"queued_pairs": 0, "message": "No subscribers."}

    pairs: list[tuple[uuid.UUID, uuid.UUID]] = []
    for sub_id in sub_ids:
        accts = db.execute(
            select(BrokerAccount.id).where(
                BrokerAccount.user_id == sub_id,
                BrokerAccount.connection_status == "connected",
            )
        ).scalars().all()
        for acct_id in accts:
            pairs.append((sub_id, acct_id))

    if not pairs:
        return {"queued_pairs": 0, "message": "No connected subscriber accounts."}

    trader_user_id = user.id
    client_ip_str = request.client.host if request.client else None

    asyncio.create_task(
        _bulk_close_subscriber_positions_background(
            pairs, trader_user_id, client_ip_str,
        )
    )

    return {
        "queued_pairs": len(pairs),
        "message": (
            f"Queued close-positions sweep across {len(pairs)} subscriber "
            "broker account(s). Positions/Orders pages will refresh "
            "live as each close lands."
        ),
    }


async def _bulk_close_subscriber_positions_background(
    pairs: list[tuple[uuid.UUID, uuid.UUID]],
    trader_user_id: uuid.UUID,
    client_ip_str: str | None,
) -> None:
    """Background coroutine for close-all-subscribers.

    Runs on the main event loop after the API response is out.
    Concurrency-limited via semaphore; per-account broker call wrapped
    in a timeout. Each per-account result audits + publishes per-order
    SSE inside ``_close_account_positions_sync``."""
    from datetime import datetime, timezone  # noqa: PLC0415
    sem = asyncio.Semaphore(_BULK_EXIT_CONCURRENCY)
    loop = asyncio.get_running_loop()
    closed_total = 0
    failed_total = 0
    started = datetime.now(timezone.utc)
    log.info(
        "bulk-close-subscribers: starting background sweep of %d pair(s) "
        "for trader=%s (concurrency=%d, per-call timeout=%.0fs)",
        len(pairs), trader_user_id, _BULK_EXIT_CONCURRENCY, _BULK_EXIT_BROKER_TIMEOUT_S,
    )

    async def _one(sub_id: uuid.UUID, acct_id: uuid.UUID) -> dict:
        async with sem:
            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(
                        None, _close_account_positions_sync,
                        sub_id, acct_id, trader_user_id, client_ip_str,
                    ),
                    timeout=_BULK_EXIT_BROKER_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "bulk-close-subscribers: timeout on sub=%s acct=%s after %.0fs",
                    sub_id, acct_id, _BULK_EXIT_BROKER_TIMEOUT_S,
                )
                return {"closed": [], "failed": [{"reason": "timeout"}]}
            except Exception:  # noqa: BLE001
                log.exception(
                    "bulk-close-subscribers: worker crashed for sub=%s acct=%s",
                    sub_id, acct_id,
                )
                return {"closed": [], "failed": [{"reason": "crashed"}]}

    results = await asyncio.gather(*(_one(s, a) for s, a in pairs))
    for r in results:
        closed_total += len(r.get("closed", []))
        failed_total += len(r.get("failed", []))

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    log.info(
        "bulk-close-subscribers: done — closed=%d failed=%d pairs=%d "
        "elapsed=%.1fs for trader=%s",
        closed_total, failed_total, len(pairs), elapsed, trader_user_id,
    )


def _marketable_option_close_price(
    adapter, pos: BrokerPosition, side: OrderSide,
) -> Decimal | None:
    """Marketable-limit price to close an option NOW: hit the bid on a SELL,
    lift the ask on a BUY, rounded to a valid option tick. Returns None if the
    contract can't be quoted — the caller then falls back to a MARKET order.

    Why: Alpaca REJECTS option MARKET orders ("no available quote", 40310000), so
    on Alpaca a same-day-expiry option can only be flattened via a limit priced
    through the market. Webull/SnapTrade accept either, and a marketable limit
    fills just like a market there too — so this is safe for EVERY broker."""
    if not hasattr(adapter, "get_option_latest_quote"):
        return None
    try:
        from app.brokers.alpaca import build_occ_symbol  # noqa: PLC0415
        occ = build_occ_symbol(
            pos.symbol, pos.option_expiry, pos.option_strike, pos.option_right.value
        )
        bid, ask = adapter.get_option_latest_quote(occ)
    except Exception:  # noqa: BLE001
        return None
    px = bid if side == OrderSide.SELL else ask
    if px is None or px <= 0:
        return None
    from app.services.trader_bracket_monitor import _round_close_limit  # noqa: PLC0415
    return _round_close_limit(px, side)


def _option_close_limit(adapter, pos: BrokerPosition, side: OrderSide) -> Decimal:
    """A LIMIT price to close an option NOW — ALWAYS non-None, so we NEVER send a
    market order for an option. Both brokers refuse option market orders: Alpaca
    always (no available quote), Webull on limited-liquidity contracts (e.g. a
    deep-OTM 0DTE — "This contract has limited liquidity and does not support
    market or stop orders"). Priority: marketable price (bid/ask) → the position's
    mark → a minimum tick. Worst case the order simply RESTS (it fills if a bid
    appears, otherwise the option cash-settles at expiration) instead of being
    rejected outright."""
    px = _marketable_option_close_price(adapter, pos, side)
    if px is not None and px > 0:
        return px
    mark = pos.current_price
    if mark is not None and mark > 0:
        from app.services.trader_bracket_monitor import _round_close_limit  # noqa: PLC0415
        rounded = _round_close_limit(mark, side)
        # ROUND_DOWN on a SELL can floor a sub-tick mark (e.g. 0.005) to 0.00 —
        # never return a non-positive limit.
        if rounded > 0:
            return rounded
    return Decimal("0.01")


def _market_order_type_refused(msg: str) -> bool:
    """True when a broker rejected a MARKET order because it won't accept that
    ORDER TYPE right now — not the trade itself. Covers an illiquid/limited-
    liquidity contract AND a trading halt ("market order rejected due to trading
    halt … please place a limit order instead"). In every case the broker WILL
    take a LIMIT, so the close should retry as a limit (which rests and fills
    when trading resumes) instead of failing outright."""
    m = msg.lower()
    return (
        "does not support market" in m
        or "limited liquidity" in m
        or "trading halt" in m
        or "place a limit order" in m
        or "halted" in m
    )


def _close_account_positions_sync(
    sub_id: uuid.UUID,
    acct_id: uuid.UUID,
    trader_user_id: uuid.UUID,
    client_ip_str: str | None,
    position_filter: "Callable[[BrokerPosition], bool] | None" = None,
    option_marketable_limit: bool = False,
) -> dict:
    """Synchronous worker for one (subscriber, broker_account) pair.

    Opens its OWN DB session — must never share the request-scoped
    session across threads. Returns ``{"closed": [...], "failed": [...]}``
    so the caller can aggregate without further locking.

    ``position_filter`` (optional): when given, only positions for which it
    returns True are closed. Used by the EOD safety sweep to close ONLY
    same-day-expiry options; a plain full-exit passes None (close everything).

    ``option_marketable_limit`` (optional): when True, OPTION positions are
    closed with a marketable LIMIT instead of MARKET so they also flatten on
    Alpaca (which rejects option MARKET orders). Stocks stay MARKET regardless.

    The position placement uses BackgroundTasks() as a no-op — the
    bulk-exit flow doesn't need the post-response audit hooks
    _place_trader_order normally schedules, but the function expects
    the parameter so we pass a fresh container.
    """
    from app.database import SessionLocal  # noqa: PLC0415
    closed: list[dict] = []
    failed: list[dict] = []

    with SessionLocal() as db_local:
        acct = db_local.get(BrokerAccount, acct_id)
        if acct is None or acct.connection_status != "connected":
            return {"closed": closed, "failed": failed}
        sub_user = db_local.get(User, sub_id)
        if sub_user is None:
            return {"closed": closed, "failed": failed}

        # List positions on the broker. Failures here drop the whole
        # account but leave other accounts intact.
        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
            positions = adapter.get_positions()
        except Exception as exc:  # noqa: BLE001
            failed.append({
                "subscriber_user_id": str(sub_id),
                "broker_account_id": str(acct_id),
                "symbol": None,
                "error": f"could not list positions: {exc}"[:300],
            })
            return {"closed": closed, "failed": failed}

        # Per-position close. Failures are captured per position.
        bg = BackgroundTasks()
        req_shim = _MinimalRequestShim(client_ip_str)
        for pos in positions:
            if pos.quantity == 0:
                continue
            if position_filter is not None and not position_filter(pos):
                continue
            reverse_side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
            qty = abs(pos.quantity)
            # Stocks close at MARKET (works on both brokers in regular hours).
            # OPTIONS ALWAYS close as a LIMIT, never market — both brokers refuse
            # option market orders (Alpaca always; Webull on limited-liquidity
            # contracts like a deep-OTM 0DTE). _option_close_limit always returns
            # a price (marketable → mark → floor). The `option_marketable_limit`
            # param is retained for signature stability but no longer gates this.
            close_type = OrderType.MARKET
            close_limit: Decimal | None = None
            if pos.instrument_type == InstrumentType.OPTION:
                close_type = OrderType.LIMIT
                close_limit = _option_close_limit(adapter, pos, reverse_side)
            payload = PlaceOrderIn(
                instrument_type=pos.instrument_type,
                symbol=pos.symbol,
                side=reverse_side,
                order_type=close_type,
                quantity=qty,
                limit_price=close_limit,
                stop_price=None,
                option_expiry=pos.option_expiry if pos.instrument_type == InstrumentType.OPTION else None,
                option_strike=pos.option_strike if pos.instrument_type == InstrumentType.OPTION else None,
                option_right=pos.option_right if pos.instrument_type == InstrumentType.OPTION else None,
            )
            try:
                order = _place_trader_order(
                    db_local, sub_user, payload, acct.id, bg, request=req_shim,  # type: ignore[arg-type]
                    skip_fanout=True, resolve_wash_trade=True,
                )
                closed.append({
                    "subscriber_user_id": str(sub_id),
                    "broker_account_id": str(acct_id),
                    "symbol": pos.symbol,
                    "qty": str(qty),
                    "side": reverse_side.value,
                    "order_id": str(order.id),
                })
            except Exception as exc:  # noqa: BLE001
                emsg = str(exc)
                # Safety net: the broker refused the order TYPE, not the trade —
                # e.g. "does not support market" / "limited liquidity" on an
                # illiquid contract. Retry ONCE as a plain LIMIT (options are
                # already limit, so this covers an illiquid stock) at the mark
                # price, so the close rests instead of failing outright.
                retriable = close_type == OrderType.MARKET and _market_order_type_refused(emsg)
                retry_px = pos.current_price if (pos.current_price and pos.current_price > 0) else None
                if retriable and retry_px is not None:
                    try:
                        retry_payload = payload.model_copy(update={
                            "order_type": OrderType.LIMIT, "limit_price": retry_px,
                        })
                        order = _place_trader_order(
                            db_local, sub_user, retry_payload, acct.id, bg, request=req_shim,  # type: ignore[arg-type]
                            skip_fanout=True, resolve_wash_trade=True,
                        )
                        closed.append({
                            "subscriber_user_id": str(sub_id),
                            "broker_account_id": str(acct_id),
                            "symbol": pos.symbol,
                            "qty": str(qty),
                            "side": reverse_side.value,
                            "order_id": str(order.id),
                            "note": "retried_as_limit",
                        })
                        continue
                    except Exception as exc2:  # noqa: BLE001
                        emsg = str(exc2)
                failed.append({
                    "subscriber_user_id": str(sub_id),
                    "broker_account_id": str(acct_id),
                    "symbol": pos.symbol,
                    "error": emsg[:300],
                })

    return {"closed": closed, "failed": failed}


@router.post("/{broker_symbol}/close", response_model=OrderOut)
def close_position(
    broker_symbol: str,
    payload: ClosePositionIn,
    request: Request,
    background: BackgroundTasks,
    broker_account_id: uuid.UUID = Query(..., description="Broker account holding the position"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Order:
    """Place a reverse-side order to close the position on the given account.

    `broker_symbol` is the broker's canonical id — OCC for options, plain
    ticker for stocks — which uniquely identifies a position even when the
    same root (e.g. AAPL stock + AAPL option) is held simultaneously.

    Re-reads the live position from the broker so the close size and side are
    based on what actually exists right now, not stale client data. For a
    trader this fans out to subscribers; for a subscriber it just runs
    against their own broker.
    """
    acct = db.get(BrokerAccount, broker_account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "broker_account_not_found")
    if acct.connection_status != "connected":
        raise HTTPException(409, "broker_not_connected")

    creds = decrypt_json(acct.encrypted_credentials)
    adapter = adapter_for(acct, creds)
    positions = adapter.get_positions()

    target = broker_symbol.upper()
    pos = next((p for p in positions if p.broker_symbol.upper() == target), None)
    if pos is None or pos.quantity == 0:
        raise HTTPException(404, "position_not_found")

    # Reverse the side based on the current holding (long → sell, short → buy).
    reverse_side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
    full_qty = abs(pos.quantity)
    close_qty = payload.quantity if payload.quantity is not None else full_qty
    if close_qty <= 0:
        raise HTTPException(422, "quantity_must_be_positive")
    if close_qty > full_qty:
        raise HTTPException(422, "quantity_exceeds_position")

    # Options can't be closed with a market order — Alpaca rejects them always,
    # Webull rejects them on limited-liquidity contracts ("does not support
    # market or stop orders"). Force a LIMIT priced through the market (or a
    # floor) UNLESS the caller supplied their own limit price.
    close_type = payload.order_type
    close_limit = payload.limit_price
    if pos.instrument_type == InstrumentType.OPTION and (
        close_type == OrderType.MARKET or close_limit is None
    ):
        close_type = OrderType.LIMIT
        close_limit = _option_close_limit(adapter, pos, reverse_side)

    # For options, _place_trader_order rebuilds the OCC symbol from
    # (expiry, strike, right), so we pass the bare root in `symbol`.
    new_payload = PlaceOrderIn(
        instrument_type=pos.instrument_type,
        symbol=pos.symbol,
        side=reverse_side,
        order_type=close_type,
        quantity=close_qty,
        limit_price=close_limit,
        stop_price=None,
        option_expiry=pos.option_expiry if pos.instrument_type == InstrumentType.OPTION else None,
        option_strike=pos.option_strike if pos.instrument_type == InstrumentType.OPTION else None,
        option_right=pos.option_right if pos.instrument_type == InstrumentType.OPTION else None,
    )

    try:
        return _place_trader_order(
            db, user, new_payload, acct.id, background, request, resolve_wash_trade=True,
        )
    except Exception as exc:  # noqa: BLE001
        # Safety net: the broker refused the MARKET order TYPE (illiquid, or a
        # trading halt — "please place a limit order instead"), not the trade.
        # Retry ONCE as a LIMIT at the mark so the close rests and fills when
        # trading resumes. Options already close as LIMIT, so this only bites a
        # halted/illiquid stock.
        retry_px = pos.current_price if (pos.current_price and pos.current_price > 0) else None
        if close_type == OrderType.MARKET and retry_px and _market_order_type_refused(str(exc)):
            retry_payload = new_payload.model_copy(
                update={"order_type": OrderType.LIMIT, "limit_price": retry_px}
            )
            return _place_trader_order(
                db, user, retry_payload, acct.id, background, request, resolve_wash_trade=True,
            )
        raise
