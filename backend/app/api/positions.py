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
import time
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user, require_sell_all_access, require_trader
from app.api.trades import _place_trader_order
from collections.abc import Callable
from decimal import Decimal

from app.brokers import adapter_for
from app.brokers.capabilities import capabilities_for
from app.brokers.base import BrokerPosition
from app.database import get_db
from app.models.broker_account import BrokerAccount
from datetime import date, datetime, timezone
from app.models.order import InstrumentType, OptionRight, Order, OrderSide, OrderStatus, OrderType
from app.models.settings import SubscriberSettings
from app.models.user import User, UserRole
from app.schemas.order import OrderOut, PlaceOrderIn
from app.schemas.position import (
    AveragePositionIn,
    ClosePositionIn,
    PositionChannelIn,
    PositionOut,
    PositionsPayload,
    StaleAccount,
    UnreachableAccount,
)
from app.models.sell_all_snapshot import SellAllSnapshot
from app.services import copy_engine, events, trailing_stop_close
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


@router.get("", response_model=None)
def list_positions(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    detail: bool = Query(
        False,
        description="Return {positions, unreachable} instead of a bare list.",
    ),
) -> "list[PositionOut] | PositionsPayload":
    """Return positions across every connected broker account for the caller.

    A position appears once per (broker_account, symbol). Disconnected accounts
    are skipped. One broker's outage never blanks the whole list.

    But a skipped account is NOT the same as an empty one, and this endpoint
    used to report them identically — 200 with the account simply absent. The
    UI cannot tell those apart, so a failed read rendered as "you hold
    nothing". That is what showed subscribers an empty positions table whenever
    Webull answered 429 (prod, 2026-09-21) while they held real positions.

    ``?detail=1`` returns ``{positions, unreachable}`` so the caller can say so.
    The bare list stays the default: three other callers depend on that shape
    and none of them needs the distinction.

    One honest limit: this reports accounts whose adapter RAISED. An adapter
    that swallows its own failure and returns [] — SnapTrade's get_positions
    does exactly that — still looks flat from here. Fixing that means changing
    those adapters to raise, which is a larger change than this one.
    """
    accts = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.connection_status == "connected",
        )
    ).scalars().all()

    out: list[PositionOut] = []
    unreachable: list[UnreachableAccount] = []
    stale: list[StaleAccount] = []
    for acct in accts:
        try:
            creds = decrypt_json(acct.encrypted_credentials)
            adapter = adapter_for(acct, creds)
            # Whether this broker's per-position Day P&L is its own native field
            # (so the row can be labelled authoritative rather than derived).
            day_src = (
                "broker_native"
                if capabilities_for(acct.broker).authoritative_position_day_pnl
                else None
            )
            prev_close_fn = getattr(adapter, "get_stock_prev_close", None)
            # Display path: let concurrent readers share one broker call.
            # Webull rejects simultaneous position reads with 429, and this
            # endpoint is called up to four times per order event by the
            # positions table alone, plus the calendar independently.
            held = adapter.get_positions(cached_ok=True)
            stale_age = getattr(held, "stale_age_s", None)
            if stale_age is not None:
                # Rate limited: these are the last positions read, not live.
                stale.append(StaleAccount(
                    broker_account_id=acct.id, broker=acct.broker.value,
                    label=acct.label, age_s=int(stale_age),
                ))
            for p in held:
                # Reference = previous session's market CLOSE for this stock.
                ref = None
                if prev_close_fn is not None and p.instrument_type == InstrumentType.STOCK:
                    try:
                        ref = prev_close_fn(p.symbol)
                    except Exception:  # noqa: BLE001
                        ref = None
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
                    open_pnl_pct=p.open_pnl_pct,
                    day_pnl=p.day_pnl,
                    day_pnl_pct=p.day_pnl_pct,
                    day_pnl_source=(day_src if p.day_pnl is not None else None),
                    reference_price=ref,
                    option_expiry=p.option_expiry,
                    option_strike=p.option_strike,
                    option_right=p.option_right,
                ))
        except Exception as exc:  # noqa: BLE001
            # Best-effort: one broker's outage shouldn't blank the whole table.
            # But it must not be SILENT either — this swallows everything, so a
            # throttle, a decrypt failure or an adapter raise all render as "no
            # positions", which is indistinguishable from a flat account and
            # leaves nothing to debug from. Log which account and why.
            log.warning(
                "positions: skipping account %s (%s) — %s",
                acct.id, acct.broker.value, str(exc)[:300], exc_info=True,
            )
            unreachable.append(UnreachableAccount(
                broker_account_id=acct.id,
                broker=acct.broker.value,
                label=acct.label,
                detail=_unreachable_detail(exc),
            ))
            continue
    # After every account's positions are in, so one query covers them all.
    #
    # Isolated: this is a display column, and this endpoint is how a trader
    # CLOSES a position. The unreachable-account handling above exists so one
    # bad broker cannot blank the list; an exception here would undo that for a
    # different reason. Positions still render, just without the channel.
    try:
        _attach_position_channels(db, user.id, out)
    except Exception:  # noqa: BLE001
        log.warning("positions: could not attach discord channels", exc_info=True)
    try:
        _attach_ladder_stops(db, user.id, out)
    except Exception:  # noqa: BLE001
        log.warning("positions: could not attach ladder stops", exc_info=True)
    if detail:
        return PositionsPayload(positions=out, unreachable=unreachable, stale=stale)
    return out


def _unreachable_detail(exc: Exception) -> str:
    """A short, user-safe reason for a failed position read.

    Never the raw exception text — it carries account ids and request ids that
    have no place in a UI. Throttling gets its own wording because it is
    transient and self-healing, so the right message is 'retrying', not 'error'.
    """
    msg = str(exc)
    if "429" in msg or "TOO_MANY_REQUESTS" in msg.upper():
        return "Rate limited by the broker — retrying"
    low = msg.lower()
    if "timeout" in low or "timed out" in low:
        return "Broker timed out — retrying"
    if "401" in msg or "403" in msg or "unauthor" in low:
        return "Broker rejected our credentials — reconnect this account"
    return "Broker unavailable — retrying"


@router.get("/today-realized")
def today_realized(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, float]:
    """Today's P&L for the positions-page summary strip — the SAME realized value
    the Calendar shows for today (closed trades, FIFO), so the two never disagree.
    Matches the broker's daily P&L; the open-position unrealized swing is
    deliberately excluded (see calendar_pnl).

    Subscribers keep mirror de-duplication via ``mirrors_only``. The response key
    stays ``realized_pnl`` for the frontend.
    """
    from app.services import market_hours
    from app.services.pnl import calendar_series

    today = market_hours.now_et().date()
    mirrors = user.role == UserRole.SUBSCRIBER
    series = calendar_series(
        db, user.id, today, today, tz_name=None, mirrors_only=mirrors,
    )
    day = series.get(today)
    return {"realized_pnl": float(day.realized_pnl) if day else 0.0}


@router.get("/day-pnl")
def account_day_pnl(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Account-level Day's P&L for TODAY — the SAME broker-aware value today's
    Calendar cell shows (Webull total_day_profit_loss / Alpaca equity−last_equity),
    produced by the calendar's OWN live resolver (no formula is recreated here).
    This is NOT realized P&L and NOT a sum of position P&L. ``day_pnl`` is None
    when the broker exposes no live day figure (UI shows '--'); a genuine broker
    0.00 comes back as 0.0; a failed live fetch falls back to the last-known
    broker value flagged stale — mirroring the calendar exactly."""
    from app.api.trades import _last_marked_snapshot, _live_day_pnl_today  # reuse resolver
    from app.services import market_hours  # noqa: PLC0415
    today = market_hours.now_et().date()
    res = _live_day_pnl_today(db, user.id)  # (value, pct, source) | (None, None, source) | None
    if res is not None and res[0] is not None:
        return {"day_pnl": float(res[0]),
                "day_pnl_pct": float(res[1]) if res[1] is not None else None,
                "source": res[2], "quality": "authoritative"}
    if res is not None and res[0] is None:
        stale = _last_marked_snapshot(db, user.id, today)
        if stale is not None:
            return {"day_pnl": float(stale[0]),
                    "day_pnl_pct": float(stale[1]) if stale[1] is not None else None,
                    "source": res[2], "quality": "stale"}
    return {"day_pnl": None, "day_pnl_pct": None, "source": "none", "quality": "unavailable"}


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
    reentry_percent: Decimal | None = Query(
        default=None, gt=0, le=100,
        description="Default re-entry: pre-set each snapshotted position to re-buy this % below its exit price, so Re-Enter uses it without re-typing. Omit = default to market.",
    ),
    trail_basis: str | None = Query(
        default=None,
        description="What trail_percent is measured from: 'current' (percent trail off the live price) or 'reference' (a dollar trail = trail_percent% of the previous market close). Omit = current.",
    ),
    reentry_basis: str | None = Query(
        default=None,
        description="Default basis for the baked-in re-entry %: 'current' (live price) or 'reference' (previous market close). Stored on the snapshot so Re-Enter uses it without re-choosing. Omit = current.",
    ),
    take_profit_percent: Decimal | None = Query(
        default=None, gt=0, le=1000,
        description="Take-profit variation: instead of closing at market/trailing, rest a LIMIT to close IN PROFIT at this % off the live price — a long sells at current×(1+%/100), a short buys back at current×(1−%/100). Stock-only; options/no-price fall back to the normal close. Mutually exclusive with trail_percent.",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(require_sell_all_access),
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
            # Record what we held (signed qty + exit price) for Re-Enter, with the
            # optional default re-entry chosen at exit time. Appended to the
            # snapshot only if the close actually places (see below) — a position
            # that fails to exit must NOT end up in the re-entry basket.
            _item = _snapshot_item(pos)
            if reentry_percent is not None:
                _item["default_mode"] = "pct"
                _item["default_value"] = str(reentry_percent)
                _item["default_basis"] = reentry_basis or "current"
            elif reentry_basis == "exit":
                # "Exit price" default with no % — re-enter AT the exit price.
                _item["default_mode"] = "pct"
                _item["default_value"] = None
                _item["default_basis"] = "exit"
            # else leaves the _snapshot_item defaults (market / None / None).
            # Stocks close at MARKET; OPTIONS always as a LIMIT — both brokers
            # refuse option market orders (Alpaca always; Webull on
            # limited-liquidity contracts). See _option_close_limit.
            close_type = OrderType.MARKET
            close_limit: Decimal | None = None
            close_trail: Decimal | None = None
            close_trail_price: Decimal | None = None
            close_method = "market"
            if (
                take_profit_percent is not None
                and take_profit_percent > 0
                and pos.current_price is not None
                and pos.current_price > 0
            ):
                # Take-profit: rest a LIMIT to close in profit rather than sell now.
                # Long → sell HIGHER (current × (1 + %/100)); short → buy back LOWER
                # (current × (1 − %/100)). Works for stocks AND options (Alpaca's
                # positions feed carries current_price for both). Only positions
                # with no live price fall through to the market/limit close.
                cur = Decimal(pos.current_price)
                factor = (Decimal(1) + take_profit_percent / Decimal(100)) if pos.quantity > 0 \
                    else (Decimal(1) - take_profit_percent / Decimal(100))
                tp_limit = (cur * factor).quantize(Decimal("0.01"))
                if tp_limit > 0:
                    close_type = OrderType.LIMIT
                    close_limit = tp_limit
                    close_method = "take_profit_limit"
            elif (
                trail_percent is not None
                and trailing_stop_close.trailing_stop_supported(adapter, pos)
            ):
                # Sell-All trailing-stop variation, where the broker supports it.
                close_type = OrderType.TRAILING_STOP
                close_method = "trailing_stop"
                if trail_basis == "reference":
                    # Dollar trail = trail_percent% of the previous market close.
                    ref = None
                    _pc = getattr(adapter, "get_stock_prev_close", None)
                    if _pc is not None:
                        try:
                            ref = _pc(pos.symbol)
                        except Exception:  # noqa: BLE001
                            ref = None
                    if ref is not None and ref > 0:
                        close_trail_price = (ref * trail_percent / Decimal(100)).quantize(Decimal("0.01"))
                        if close_trail_price <= 0:
                            # Trail too small to be a valid $ amount (rounds to $0)
                            # — fall back to a percent trail off the live price.
                            close_trail_price = None
                            close_trail = trail_percent
                    else:
                        close_trail = trail_percent   # fall back to percent trail
                elif trail_basis == "exit":
                    # Dollar trail = trail_percent% of the current (exit-time) price.
                    cur = pos.current_price
                    if cur is not None and cur > 0:
                        close_trail_price = (Decimal(cur) * trail_percent / Decimal(100)).quantize(Decimal("0.01"))
                        if close_trail_price <= 0:
                            close_trail_price = None
                            close_trail = trail_percent
                    else:
                        close_trail = trail_percent   # no price → percent trail
                else:
                    close_trail = trail_percent       # 'current' = percent trail off the live price
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
                trail_price=close_trail_price,
                option_expiry=pos.option_expiry if pos.instrument_type == InstrumentType.OPTION else None,
                option_strike=pos.option_strike if pos.instrument_type == InstrumentType.OPTION else None,
                option_right=pos.option_right if pos.instrument_type == InstrumentType.OPTION else None,
            )
            try:
                if reverse_side == OrderSide.SELL:
                    # Free a resting Discord ladder stop first (see close_position).
                    from app.services import discord_stop_orders  # noqa: PLC0415

                    discord_stop_orders.release_for_position(db, user, pos)
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
                # Only a position we actually placed a close for goes into the
                # re-entry snapshot.
                snapshot_positions.append(_item)
            except Exception as exc:  # noqa: BLE001
                failed.append({
                    "broker_account_id": str(acct.id),
                    "symbol": pos.symbol,
                    "error": str(exc)[:300],
                })

    snapshot_id = None
    if snapshot_positions:
        # Join today's snapshot (one per day) so the whole day is a single table,
        # not a separate one per Exit-All. Individual closes append the same way.
        snap = _capture_exit_snapshot(db, user.id, snapshot_positions, new_event=False)
        db.commit()
        snapshot_id = str(snap.id) if snap else None

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


def _attach_position_channels(db: Session, user_id, positions: list) -> None:
    """Set .discord_channel on positions a Discord alert opened.

    A position is the broker's, not ours, so there is no order id on it — the
    link has to be made by CONTRACT. For each held contract we take the most
    recent Discord ENTRY, and its channel.

    ONE query, narrowed to the symbols actually held, so a trader with a long
    Discord history does not pay for all of it on every positions refresh —
    this endpoint is called up to four times per order event.

    "Most recent" matters: re-entering the same contract from a different
    channel should show the channel that opened the position you are holding
    NOW, not the first one that ever traded it.

    Self on top of another channel reads "<Channel>-Self" (e.g. "Clint-Self"):
    the trader added by hand to a position a channel opened, and both are in
    it. Only entries of the CURRENT holding count — from the order that opened
    the contract's live exit ladder on — so a Clint trade on an earlier, closed
    holding of the same contract is never glued onto a fresh Self one. With no
    live ladder there is no way to tell holdings apart, and the most recent
    entry's channel is shown as before.
    """
    if not positions:
        return
    from app.models.discord_alert_source import DiscordAlertSource  # noqa: PLC0415
    from app.models.discord_message import DiscordMessage  # noqa: PLC0415
    from app.models.discord_position_guard import DiscordPositionGuard  # noqa: PLC0415
    from app.models.order import Order, OrderSide, OrderStatus  # noqa: PLC0415
    from sqlalchemy import func  # noqa: PLC0415
    from app.api.discord_sources import _SELF_CHANNEL_ID  # noqa: PLC0415

    for p in positions:
        p.discord_channel = None
    symbols = {(p.symbol or "").upper() for p in positions if p.symbol}
    if not symbols:
        return

    placed_at = func.coalesce(Order.submitted_at, Order.created_at)
    rows = db.execute(
        select(
            Order.symbol, Order.instrument_type, Order.option_expiry,
            Order.option_strike, Order.option_right,
            DiscordAlertSource.label, DiscordAlertSource.channel_name,
            DiscordAlertSource.channel_id, placed_at,
        )
        .join(DiscordMessage, DiscordMessage.order_id == Order.id)
        .join(DiscordAlertSource, DiscordAlertSource.id == DiscordMessage.source_id,
              isouter=True)
        .where(
            Order.user_id == user_id,
            Order.symbol.in_(symbols),
            Order.side == OrderSide.BUY,
            Order.is_closing.is_(False),
            Order.status.in_((OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)),
        )
        # Newest first, so the first row seen per contract is the one that wins.
        .order_by(placed_at.desc())
    ).all()

    # Where each contract's current holding began: the order that opened its
    # live ladder, else the ladder's own creation.
    holding_start: dict = {}
    for g_sym, g_strike, g_right, g_expiry, g_created, entry_at in db.execute(
        select(
            DiscordPositionGuard.symbol, DiscordPositionGuard.option_strike,
            DiscordPositionGuard.option_right, DiscordPositionGuard.option_expiry,
            DiscordPositionGuard.created_at,
            func.coalesce(Order.submitted_at, Order.created_at),
        )
        .join(Order, Order.id == DiscordPositionGuard.entry_order_id, isouter=True)
        .where(
            DiscordPositionGuard.user_id == user_id,
            DiscordPositionGuard.closed_at.is_(None),
            DiscordPositionGuard.symbol.in_(symbols),
        )
    ).all():
        right = getattr(g_right, "value", g_right) or None
        holding_start[((g_sym or "").upper(), g_strike, right, g_expiry)] = entry_at or g_created

    # contract -> [(name, is_self, placed_at)], newest first
    entries: dict = {}
    for sym, itype, expiry, strike, right, label, channel_name, channel_id, at in rows:
        name = (label or "").strip() or (channel_name or "").strip()
        if not name:
            continue
        key = ((sym or "").upper(), itype, expiry, strike, right)
        entries.setdefault(key, []).append((name, channel_id == _SELF_CHANNEL_ID, at))

    for p in positions:
        right = getattr(p.option_right, "value", p.option_right) or None
        key = (
            (p.symbol or "").upper(), p.instrument_type,
            p.option_expiry, p.option_strike, p.option_right,
        )
        found = entries.get(key)
        if not found:
            continue
        p.discord_channel = found[0][0]
        start = holding_start.get(((p.symbol or "").upper(), p.option_strike, right, p.option_expiry))
        if start is None:
            continue
        current = [e for e in found if e[2] is not None and e[2] >= start]
        others = [e for e in current if not e[1]]
        if others and any(e[1] for e in current):
            # The channel that opened the holding: the oldest non-Self entry.
            p.discord_channel = f"{others[-1][0]}-Self"

    # A channel the trader assigned by hand wins over everything derived above —
    # including for a position no alert opened at all.
    assigned = {
        ((sym or "").upper(), strike, getattr(right, "value", right) or None, expiry):
            (label or "").strip() or (channel_name or "").strip()
        for sym, strike, right, expiry, label, channel_name in db.execute(
            select(
                DiscordPositionGuard.symbol, DiscordPositionGuard.option_strike,
                DiscordPositionGuard.option_right, DiscordPositionGuard.option_expiry,
                DiscordAlertSource.label, DiscordAlertSource.channel_name,
            )
            .join(DiscordAlertSource, DiscordAlertSource.id == DiscordPositionGuard.source_id)
            .where(
                DiscordPositionGuard.user_id == user_id,
                DiscordPositionGuard.closed_at.is_(None),
                DiscordPositionGuard.symbol.in_(symbols),
            )
        ).all()
    }
    for p in positions:
        name = assigned.get((
            (p.symbol or "").upper(), p.option_strike,
            getattr(p.option_right, "value", p.option_right) or None, p.option_expiry,
        ))
        if name:
            p.discord_channel = name


def _attach_ladder_stops(db: Session, user_id, positions: list) -> None:
    """Set .ladder_stop_price from each held contract's live Discord guard.

    One query over the live guards for the symbols held. A display column, so
    the caller isolates failures the same way as the channel column.
    """
    for p in positions:
        p.ladder_stop_price = None
    symbols = {(p.symbol or "").upper() for p in positions if p.symbol}
    if not symbols:
        return
    from app.models.discord_position_guard import DiscordPositionGuard  # noqa: PLC0415

    rows = db.execute(
        select(DiscordPositionGuard).where(
            DiscordPositionGuard.user_id == user_id,
            DiscordPositionGuard.closed_at.is_(None),
            DiscordPositionGuard.stop_price.is_not(None),
            DiscordPositionGuard.symbol.in_(symbols),
        )
    ).scalars()
    by_contract = {
        (g.symbol, g.option_strike, (g.option_right or None), g.option_expiry): g.stop_price
        for g in rows
    }
    for p in positions:
        right = getattr(p.option_right, "value", p.option_right)
        p.ladder_stop_price = by_contract.get(
            ((p.symbol or "").upper(), p.option_strike, right or None, p.option_expiry)
        )


def _reentry_info(db: Session, item: dict) -> "tuple[str, Decimal | None, str | None]":
    """(status, reentry_price, filled_at) for a snapshot item.
      status: 'filled' / 'working' / 'pending' (see below).
      reentry_price: what we re-entered at — the FILL price when filled, the
                     resting LIMIT price when working, else None.
      filled_at: ISO time the buy-back filled (order.closed_at), else None.
    'filled'  — the buy-back filled; the position is back.
    'working' — a buy-back is resting (waiting to fill).
    'pending' — never re-entered, or the last attempt canceled/expired/rejected —
                so it still NEEDS a (re-)entry (only 'pending' items get placed)."""
    oid = item.get("reentry_order_id")
    if not oid:
        return "pending", None, None
    o = db.get(Order, uuid.UUID(oid))
    if o is None:
        return "pending", None, None
    if o.status == OrderStatus.FILLED:
        return "filled", o.filled_avg_price, (o.closed_at.isoformat() if o.closed_at else None)
    if o.status in _REENTRY_WORKING:
        return "working", o.limit_price, None   # resting at this limit (None for market)
    return "pending", None, None  # canceled / expired / rejected → re-enter allowed again


def _reentry_status(db: Session, item: dict) -> str:
    """Just the status (see _reentry_info)."""
    return _reentry_info(db, item)[0]


def _snapshot_item(pos: BrokerPosition) -> dict:
    """A snapshot position row from a live BrokerPosition (signed qty + exit price)."""
    return {
        "symbol": pos.symbol,
        "instrument_type": pos.instrument_type.value,
        "quantity": str(pos.quantity),
        "price": str(pos.current_price) if pos.current_price is not None else None,
        "option_expiry": pos.option_expiry.isoformat() if pos.option_expiry else None,
        "option_strike": str(pos.option_strike) if pos.option_strike is not None else None,
        "option_right": pos.option_right.value if pos.option_right else None,
        "reentry_order_id": None,
        "default_mode": "market",   # re-entry default chosen at exit ("market"/"pct"/"limit")
        "default_value": None,      # the % or $ for the default
        "default_basis": None,      # "current"/"reference" for a "pct" default
    }


def _snap_key(p: dict) -> tuple:
    """Identity of a snapshot item — symbol + option contract parts."""
    return (p["symbol"], p.get("option_expiry"), p.get("option_strike"), p.get("option_right"))


def _capture_exit_snapshot(
    db: Session, user_id: uuid.UUID, items: list[dict], new_event: bool = True,
) -> "SellAllSnapshot | None":
    """Record exited positions into a re-entry snapshot.

    ``new_event=False`` (the default flow for both Exit-All and single closes)
    APPENDS to today's active snapshot, so the whole day's exits collect into ONE
    snapshot / one table rather than fragmenting per exit; it only starts a new
    snapshot when there's no active one from today (i.e. the first exit of a new
    day). ``new_event=True`` forces a fresh snapshot instead. Items are deduped by
    contract identity. Older days' snapshots are retained in the history."""
    if not items:
        return None

    active = db.execute(
        select(SellAllSnapshot).where(
            SellAllSnapshot.user_id == user_id, SellAllSnapshot.active.is_(True)
        ).order_by(SellAllSnapshot.created_at.desc())
    ).scalars().all()

    # A single close joins today's current snapshot instead of spawning its own.
    if not new_event and active:
        from app.services import market_hours as _mh  # noqa: PLC0415
        today_et = _mh.now_et().date()
        snap = active[0]
        if snap.created_at.astimezone(_mh.ET).date() == today_et:
            for s in active[1:]:
                s.active = False
            by_key = {_snap_key(it): it for it in snap.positions}
            for it in items:
                by_key[_snap_key(it)] = it   # add / refresh this contract
            snap.positions = list(by_key.values())   # reassign so JSONB persists
            db.flush()
            return snap

    # New event (Exit-All), or no usable active snapshot: start a fresh one.
    for s in active:
        s.active = False
    by_key: dict = {}
    for it in items:
        by_key[_snap_key(it)] = it   # dedup within this one exit event
    snap = SellAllSnapshot(user_id=user_id, positions=list(by_key.values()), active=True)
    db.add(snap)
    db.flush()
    return snap


def _snapshot_adapter(db: Session, user: User):
    """Best-effort broker adapter for a user's connected account — used for the
    live Current price / PDC columns. None when nothing is connected or creds
    won't decrypt (the detail then shows "—" instead of erroring)."""
    acct = db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id, BrokerAccount.connection_status == "connected",
        )
    ).scalars().first()
    if acct is None:
        return None
    try:
        return adapter_for(acct, decrypt_json(acct.encrypted_credentials))
    except Exception:  # noqa: BLE001
        return None


def _snapshot_detail(db: Session, snap: SellAllSnapshot, adapter, today_et: date) -> dict:
    """Turn one snapshot row into the priced detail the Snapshot page renders:
    each position's live re-entry status (filled / working / pending / expired),
    current price, PDC, and a summary count. `adapter` may be None (prices show
    "—"); `today_et` decides option expiry. Shared by the `latest` and `today`
    endpoints so both price identically."""
    def _current(p: dict) -> str | None:
        """Live price for the Current Price column. Stocks use the stock quote;
        options use the OCC bid/ask mid (Alpaca OPRA). Best-effort — "—" when a
        contract can't be quoted (illiquid / after-hours) or the broker lacks the
        method."""
        if adapter is None:
            return None
        itype = p["instrument_type"]
        if itype == "stock":
            fn = getattr(adapter, "get_stock_latest_price", None)
            if fn is None:
                return None
            try:
                px = fn(p["symbol"])
                return str(px) if px is not None else None
            except Exception:  # noqa: BLE001
                return None
        # Option: mid of the OCC bid/ask. Skips cleanly on non-Alpaca adapters
        # (no get_option_latest_quote) or when the contract details are missing.
        if itype == "option" and p.get("option_expiry") and p.get("option_strike") and p.get("option_right"):
            fn = getattr(adapter, "get_option_latest_quote", None)
            if fn is None:
                return None
            try:
                from app.brokers.alpaca import build_occ_symbol  # noqa: PLC0415
                occ = build_occ_symbol(
                    p["symbol"], date.fromisoformat(p["option_expiry"]),
                    Decimal(p["option_strike"]), p["option_right"],
                )
                bid, ask = fn(occ)
                mid = (bid + ask) / Decimal(2) if bid is not None and ask is not None \
                    else (ask if ask is not None else bid)
                return str(mid.quantize(Decimal("0.01"))) if mid is not None else None
            except Exception:  # noqa: BLE001
                return None
        return None

    def _pdc(sym: str, itype: str) -> str | None:
        """Previous day's market close — the PDC re-entry basis."""
        fn = getattr(adapter, "get_stock_prev_close", None)
        if adapter is None or itype != "stock" or fn is None:
            return None
        try:
            px = fn(sym)
            return str(px) if px is not None else None
        except Exception:  # noqa: BLE001
            return None

    # An expired option can't be re-bought (the contract is gone). Flag it as a
    # distinct "expired" status so the row disables Re-Enter and Re-Enter All
    # skips it. Expired = expiry strictly before today ET (it's still tradeable
    # ON the expiry date until close).
    def _is_expired_option(pos: dict) -> bool:
        if pos.get("instrument_type") != "option" or not pos.get("option_expiry"):
            return False
        try:
            return date.fromisoformat(pos["option_expiry"]) < today_et
        except (ValueError, TypeError):
            return False

    positions = []
    for p in snap.positions:
        st, reentry_price, reentry_filled_at = _reentry_info(db, p)
        # Overlay expiry only on a not-yet-re-entered row; a filled/working one
        # keeps its status (it already has an order).
        if st == "pending" and _is_expired_option(p):
            st = "expired"
        positions.append({
            "symbol": p["symbol"],
            "instrument_type": p["instrument_type"],
            "quantity": p["quantity"],
            "price": p.get("price"),                                  # exit price
            "current_price": _current(p),
            "pdc": _pdc(p["symbol"], p["instrument_type"]),           # previous day close
            "reentry_price": str(reentry_price) if reentry_price is not None else None,
            "reentry_filled_at": reentry_filled_at,                   # when the buy-back filled
            "default_mode": p.get("default_mode", "market"),          # default re-entry chosen at exit
            "default_value": p.get("default_value"),
            "default_basis": p.get("default_basis"),
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
        "expired": sum(1 for x in positions if x["reentry_status"] == "expired"),
    }
    return {
        "id": str(snap.id),
        "created_at": snap.created_at.isoformat(),
        "positions": positions,
        "summary": summary,
    }


@router.get("/snapshots/latest")
def latest_sell_all_snapshot(
    snapshot_id: uuid.UUID | None = Query(
        default=None,
        description="Load a SPECIFIC snapshot from the history. Omit for the current (active/newest) one.",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(require_sell_all_access),
) -> dict:
    """One Sell-All snapshot with each position's live re-entry status
    (filled / working / pending / expired) + current price and a summary count.
    Omit `snapshot_id` for the current (active) snapshot; pass it to open any
    snapshot from the history. Null if none / not found."""
    if snapshot_id is not None:
        snap = db.get(SellAllSnapshot, snapshot_id)
        if snap is None or snap.user_id != user.id:
            return {"snapshot": None}
    else:
        snap = db.execute(
            select(SellAllSnapshot)
            .where(SellAllSnapshot.user_id == user.id, SellAllSnapshot.active.is_(True))
            .order_by(SellAllSnapshot.created_at.desc())
            .limit(1)
        ).scalars().first()
    if not snap:
        return {"snapshot": None}
    from app.services import market_hours as _mh  # noqa: PLC0415
    adapter = _snapshot_adapter(db, user)
    return {"snapshot": _snapshot_detail(db, snap, adapter, _mh.now_et().date())}


@router.get("/snapshots/today")
def today_sell_all_snapshots(
    db: Session = Depends(get_db),
    user: User = Depends(require_sell_all_access),
) -> dict:
    """Every snapshot taken TODAY (ET), newest first, each fully priced like the
    `latest` endpoint. The Snapshot page shows these stacked so a fresh Exit
    doesn't hide the earlier ones — the whole day stays on screen. History
    (`/snapshots`) is unaffected: each snapshot is still its own record."""
    from datetime import datetime, timezone  # noqa: PLC0415
    from app.services import market_hours as _mh  # noqa: PLC0415
    now_et = _mh.now_et()
    today_et = now_et.date()
    day_start_utc = datetime(today_et.year, today_et.month, today_et.day, tzinfo=_mh.ET).astimezone(timezone.utc)
    snaps = db.execute(
        select(SellAllSnapshot)
        .where(SellAllSnapshot.user_id == user.id, SellAllSnapshot.created_at >= day_start_utc)
        .order_by(SellAllSnapshot.created_at.desc())
    ).scalars().all()
    # Guard against a clock/tz edge landing a snapshot on the wrong side.
    snaps = [s for s in snaps if s.created_at.astimezone(_mh.ET).date() == today_et]
    adapter = _snapshot_adapter(db, user)   # built once for the whole day
    return {"snapshots": [_snapshot_detail(db, s, adapter, today_et) for s in snaps]}


@router.get("/snapshots")
def list_sell_all_snapshots(
    db: Session = Depends(get_db),
    user: User = Depends(require_sell_all_access),
) -> dict:
    """History of the user's Sell-All snapshots, newest first: id, when it was
    taken, position count, and a status breakdown. Cheap — counts come from the
    DB order rows + expiry, no live price calls (the per-snapshot detail endpoint
    does the pricing). `active` marks the current one."""
    from app.services import market_hours as _mh  # noqa: PLC0415
    today_et = _mh.now_et().date()

    def _expired(pos: dict) -> bool:
        if pos.get("instrument_type") != "option" or not pos.get("option_expiry"):
            return False
        try:
            return date.fromisoformat(pos["option_expiry"]) < today_et
        except (ValueError, TypeError):
            return False

    snaps = db.execute(
        select(SellAllSnapshot)
        .where(SellAllSnapshot.user_id == user.id)
        .order_by(SellAllSnapshot.created_at.desc())
    ).scalars().all()

    items = []
    for snap in snaps:
        counts = {"filled": 0, "working": 0, "pending": 0, "expired": 0}
        for p in snap.positions:
            st = _reentry_status(db, p)
            if st == "pending" and _expired(p):
                st = "expired"
            counts[st] = counts.get(st, 0) + 1
        items.append({
            "id": str(snap.id),
            "created_at": snap.created_at.isoformat(),
            "active": snap.active,
            "total": len(snap.positions),
            **counts,
        })
    return {"snapshots": items}


@router.delete("/snapshots/{snapshot_id}")
def delete_sell_all_snapshot(
    snapshot_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_sell_all_access),
) -> dict:
    """Delete one snapshot from the history. Only removes the re-entry record —
    it never touches any orders that were already placed."""
    snap = db.get(SellAllSnapshot, snapshot_id)
    if snap is None or snap.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="snapshot_not_found")
    db.delete(snap)
    db.commit()
    return {"ok": True, "deleted": str(snapshot_id)}


@router.delete("/snapshots/{snapshot_id}/positions/{index}")
def delete_snapshot_position(
    snapshot_id: uuid.UUID,
    index: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_sell_all_access),
) -> dict:
    """Remove ONE order (by 0-based index) from a snapshot's re-entry list. If it
    was the last one, the whole snapshot is deleted. Only touches the re-entry
    record — never any orders already placed."""
    snap = db.get(SellAllSnapshot, snapshot_id)
    if snap is None or snap.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="snapshot_not_found")
    poss = list(snap.positions)
    if index < 0 or index >= len(poss):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="position_not_found")
    del poss[index]
    if not poss:
        db.delete(snap)
        db.commit()
        return {"ok": True, "remaining": 0, "snapshot_deleted": True}
    snap.positions = poss   # reassign so JSONB persists
    db.commit()
    return {"ok": True, "remaining": len(poss), "snapshot_deleted": False}


@router.post("/channel")
def assign_position_channel(
    payload: PositionChannelIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Assign a held position to one of the user's Discord channels, to Self, or
    back to "auto" (the channel whose alert opened it).

    More than a label: the assigned channel's exit settings and ladder apply to
    the position from here on, and that channel's "adding" / "stopped out"
    alerts reach it — exactly as if its alert had opened it. A position with no
    exit ladder yet (opened by hand) gets one, starting from ``entry_price``.
    """
    from app.api import discord_sources  # noqa: PLC0415 — cycle
    from app.models.discord_alert_source import DiscordAlertSource  # noqa: PLC0415
    from app.services import discord_position_guard as guards  # noqa: PLC0415

    discord_sources.require_discord_member(user=user, db=db)   # 403 without Discord trading

    is_option = payload.option_strike is not None
    args = (
        payload.symbol.upper(),
        payload.option_strike if is_option else None,
        payload.option_right if is_option else None,
        payload.option_expiry if is_option else None,
    )
    guard = guards.find(db, user.id, *args)

    choice = payload.channel.strip().lower()
    if choice == "auto":
        if guard is not None and guard.source_id is not None:
            guard.source_id = None
            db.commit()
        return {"channel": "auto", "discord_channel": None}

    if choice == "self":
        src = discord_sources._self_source(db, user)
    else:
        try:
            src = db.get(DiscordAlertSource, uuid.UUID(payload.channel))
        except ValueError:
            src = None
        if src is None or src.user_id != user.id:
            raise HTTPException(404, "channel_not_found")

    if guard is None:
        guard = guards.on_buy(db, user.id, *args, entry_price=payload.entry_price)
    guard.source_id = src.id
    db.commit()
    log.info("positions: %s assigned %s to channel %s", user.id, payload.symbol, src.id)
    name = (src.label or "").strip() or (src.channel_name or "").strip() or None
    return {"channel": str(src.id), "discord_channel": name}


@router.post("/re-enter")
def re_enter_from_snapshot(
    request: Request,
    background: BackgroundTasks,
    discount_percent: Decimal | None = Query(
        default=None, ge=0, le=100,
        description="Re-buy each position this % BELOW its exit price (a resting LIMIT). Omit / 0 = market buy now.",
    ),
    limit_price: Decimal | None = Query(
        default=None, gt=0,
        description="Exact LIMIT price to re-buy at (per-order; wins over discount_percent). Use with symbol.",
    ),
    basis: str | None = Query(
        default=None,
        description="What discount_percent is % below: 'current' (live price), 'reference' (previous market close), or 'exit' (the recorded exit price). Omit = exit price.",
    ),
    snapshot_id: uuid.UUID | None = Query(default=None, description="Which snapshot; omit for the latest."),
    symbol: str | None = Query(
        default=None,
        description="Re-enter ONLY this symbol (legacy per-order re-entry). Ambiguous when the snapshot has the same symbol twice — prefer `index`.",
    ),
    index: int | None = Query(
        default=None, ge=0,
        description="Re-enter ONLY the position at this 0-based index in the snapshot. Unambiguous even when a symbol appears more than once. Wins over `symbol`.",
    ),
    trail_percent: Decimal | None = Query(
        default=None, gt=0, le=100,
        description="Re-enter as a TRAILING-STOP order at this trail % instead of market/limit (stock only; options fall back to market). Wins over discount_percent/limit_price.",
    ),
    trail_down_percent: Decimal | None = Query(
        default=None, gt=0, le=100,
        description="'Trail down': rest a BUY LIMIT this % below the live price, then re-price it lower as the stock falls (ratchets DOWN only). Stock only; managed by trail_down_monitor. Wins over discount_percent/limit_price.",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(require_sell_all_access),
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

    adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))

    def _basis_price(item: dict) -> "Decimal | None":
        """Live price the discount is taken from. 'current' = live quote (stock
        quote, or the option bid/ask mid); 'reference' = previous close (stocks
        only — options have no PDC)."""
        sym = item["symbol"]
        is_opt = item.get("instrument_type") == "option"
        if basis == "current" and is_opt:
            fn = getattr(adapter, "get_option_latest_quote", None)
            if fn is None or not (item.get("option_expiry") and item.get("option_strike") and item.get("option_right")):
                return None
            try:
                from app.brokers.alpaca import build_occ_symbol  # noqa: PLC0415
                occ = build_occ_symbol(sym, date.fromisoformat(item["option_expiry"]),
                                       Decimal(item["option_strike"]), item["option_right"])
                bid, ask = fn(occ)
                if bid is not None and ask is not None:
                    return (bid + ask) / Decimal(2)
                return ask if ask is not None else bid
            except Exception:  # noqa: BLE001
                return None
        if is_opt:
            return None   # options have no previous-day close
        fn = None
        if basis == "current":
            fn = getattr(adapter, "get_stock_latest_price", None)
        elif basis == "reference":
            fn = getattr(adapter, "get_stock_prev_close", None)
        if fn is None:
            return None
        try:
            return fn(sym)
        except Exception:  # noqa: BLE001
            return None

    disc = Decimal(discount_percent) if discount_percent is not None else Decimal(0)
    placed: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    new_positions: list[dict] = []
    for i, p in enumerate(snap.positions):
        # Per-order re-entry: leave every other position untouched. `index`
        # pins the EXACT row (the same symbol can appear twice — e.g. two
        # separate exits — and matching on symbol alone would re-enter the
        # wrong one). `symbol` stays as a legacy fallback.
        if index is not None:
            if i != index:
                new_positions.append(p)
                continue
        elif symbol is not None and p["symbol"] != symbol:
            new_positions.append(p)
            continue
        st = _reentry_status(db, p)
        if st in ("filled", "working"):
            # Already back, or a buy-back is resting — don't place again.
            skipped.append({"symbol": p["symbol"], "reason": st})
            new_positions.append(p)
            continue
        # An expired option contract can't be re-bought — never send it to the
        # broker (guards direct API / index calls, not just the UI).
        if p.get("instrument_type") == "option" and p.get("option_expiry"):
            try:
                from app.services import market_hours as _mh  # noqa: PLC0415
                if date.fromisoformat(p["option_expiry"]) < _mh.now_et().date():
                    skipped.append({"symbol": p["symbol"], "reason": "expired"})
                    new_positions.append(p)
                    continue
            except (ValueError, TypeError):
                pass
        try:
            qty = Decimal(p["quantity"])
            side = OrderSide.BUY if qty > 0 else OrderSide.SELL
            it = InstrumentType(p["instrument_type"])
            price = Decimal(p["price"]) if p.get("price") else None
            is_buy = side == OrderSide.BUY
            is_stock = it == InstrumentType.STOCK
            # Trailing re-entry: a TRAILING_STOP that follows the price and
            # triggers the buy-back once it turns by the trail %. STOCK-only —
            # Alpaca can't trail options, so an option here falls through to a
            # limit/market re-buy.
            use_trail = trail_percent is not None and trail_percent > 0 and is_buy and is_stock
            # "Trail down": a managed BUY LIMIT resting trail_down_percent below
            # the live price; trail_down_monitor ratchets it lower as the stock
            # falls. STOCK-only (needs a live stock quote to re-price). Broker
            # sees a plain limit — the ratchet is ours.
            use_trail_down = (
                not use_trail and trail_down_percent is not None
                and trail_down_percent > 0 and is_buy and is_stock
            )
            # Limit re-buy (BUY side). Works for stocks AND options now: an
            # explicit Limit $, or a % below the chosen basis (live/PDC/exit).
            # For options only 'current' (option bid/ask mid) and 'exit' (the
            # recorded exit price) are priceable — PDC returns None → market.
            if use_trail:
                use_limit, limit_val = False, None
            elif use_trail_down:
                # Initial limit = trail_down_percent below the live price. If we
                # can't quote it, fall back to a plain market re-buy.
                live_px = _basis_price({**p, "instrument_type": "stock"}) if basis == "current" else None
                if live_px is None:
                    try:
                        fn = getattr(adapter, "get_stock_latest_price", None)
                        live_px = fn(p["symbol"]) if fn else None
                    except Exception:  # noqa: BLE001
                        live_px = None
                if live_px is not None and live_px > 0:
                    use_limit, limit_val = True, (live_px * (Decimal(1) - trail_down_percent / Decimal(100))).quantize(Decimal("0.01"))
                else:
                    use_trail_down, use_limit, limit_val = False, False, None
            elif limit_price is not None and is_buy:
                use_limit, limit_val = True, limit_price
            elif is_buy and (disc > 0 or basis == "exit"):
                base_px = _basis_price(p) if basis in ("current", "reference") else price
                if base_px is not None and base_px > 0:
                    use_limit, limit_val = True, (base_px * (Decimal(1) - disc / Decimal(100))).quantize(Decimal("0.01"))
                else:
                    use_limit, limit_val = False, None   # couldn't price it → market
            else:
                use_limit, limit_val = False, None
            payload = PlaceOrderIn(
                instrument_type=it,
                symbol=p["symbol"],
                side=side,
                order_type=OrderType.TRAILING_STOP if use_trail else (OrderType.LIMIT if use_limit else OrderType.MARKET),
                quantity=abs(qty),
                limit_price=limit_val,
                trail_percent=trail_percent if use_trail else None,
                option_expiry=date.fromisoformat(p["option_expiry"]) if p.get("option_expiry") else None,
                option_strike=Decimal(p["option_strike"]) if p.get("option_strike") else None,
                option_right=OptionRight(p["option_right"]) if p.get("option_right") else None,
            )
            order = _place_trader_order(
                db, user, payload, acct.id, background, request,
                skip_fanout=True, resolve_wash_trade=False,
            )
            # Tag the row so trail_down_monitor re-prices it. The order itself is
            # a plain limit at the broker.
            if use_trail_down:
                order.trail_down_percent = trail_down_percent
            placed.append({
                "symbol": p["symbol"], "side": side.value, "qty": str(abs(qty)),
                "order_type": payload.order_type.value,
                "limit_price": str(limit_val) if limit_val is not None else None,
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

    Not for a Discord trader: their subscribers trade independently
    (copy_engine.trades_independently).
    """
    if copy_engine.trades_independently(user):
        raise HTTPException(409, "Your subscribers trade your Discord channels on their own settings — their orders and positions are theirs, not yours to act on.")
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



# Statuses whose UNFILLED remainder still RESERVES the position at the broker.
# A resting order on a contract holds the shares/contracts behind it, so a close
# placed on top is refused — Webull:
#   OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION "This order cannot be entered
#   because it will reverse an existing position. You may need to close an open
#   position, or cancel an open order, before you can submit this order."
# Alpaca does the same thing with 40310000 (held_for_orders).
_RESERVING_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
)

# A cancel's 200 means the request was ACCEPTED, not that the broker has already
# released the reservation. Placing the close in the same breath can still hit
# the rejection we just cleared. One short pause is enough in practice and costs
# no extra API calls — deliberately not a poll, because Webull's trade endpoints
# share a ~10 req/30s budget per app_key and a verify loop would spend it at the
# exact moment the close needs it.
_CANCEL_SETTLE_S = 0.6


def _cancel_working_orders_for_position(
    db: Session, user: User, acct: BrokerAccount, adapter: Any, pos: BrokerPosition,
) -> list[uuid.UUID]:
    """Cancel every still-working order on the SAME contract before closing it.

    Closing from the positions table used to fail outright whenever anything was
    already resting on that contract — a protective stop, a take-profit, a
    partially-filled limit. The broker refuses the close because the resting
    order still reserves the position, and the user is left reading a raw
    OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION with a working stop they did not
    know was in the way.

    So clear the contract first. BOTH sides go: a resting SELL blocks the close
    directly, and a resting BUY would re-open the position moments after we
    flatten it.

    This is the trader-side twin of ``copy_engine._cancel_subscriber_conflicts``,
    which already does the same thing for mirrors — the subscriber path was
    handled and the trader's own account was not. Kept separate rather than
    shared because that one runs in a worker thread on its own session and
    excludes the mirror it is about to place.

    Best-effort per order: a cancel that fails is logged and the rest continue,
    because a stale id (already filled/cancelled at the broker) must not block a
    close the user asked for. Returns the ids actually cancelled.
    """
    rows = db.execute(
        select(Order).where(
            Order.user_id == user.id,
            Order.broker_account_id == acct.id,
            Order.instrument_type == pos.instrument_type,
            Order.symbol == pos.symbol,
            Order.option_expiry.is_not_distinct_from(pos.option_expiry),
            Order.option_strike.is_not_distinct_from(pos.option_strike),
            Order.option_right.is_not_distinct_from(pos.option_right),
            Order.status.in_(_RESERVING_STATUSES),
            Order.broker_order_id.isnot(None),
        )
    ).scalars().all()
    if not rows:
        return []

    now = datetime.now(timezone.utc)
    cancelled: list[uuid.UUID] = []
    touched: list[Order] = []
    for o in rows:
        try:
            adapter.cancel_order(o.broker_order_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "close: could not cancel %s order %s (broker_order=%s) blocking the "
                "close of %s — continuing: %s",
                o.order_type.value, o.id, o.broker_order_id, pos.broker_symbol,
                str(exc)[:200],
            )
            continue
        o.status = OrderStatus.CANCELED
        o.closed_at = now
        o.reject_reason = (
            "Cancelled automatically to free the position for a close you placed "
            "from the positions table."
        )[:480]
        cancelled.append(o.id)
        touched.append(o)

    if cancelled:
        db.commit()
        for o in touched:
            db.refresh(o)
            events.publish(user.id, copy_engine._order_event("order.cancelled", o))  # noqa: SLF001
        log.info(
            "close: cancelled %d working order(s) on %s before closing",
            len(cancelled), pos.broker_symbol,
        )
        # Give the broker a moment to release the reservation (see above).
        time.sleep(_CANCEL_SETTLE_S)
    return cancelled


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

    if payload.quantity is not None and payload.quantity <= 0:
        raise HTTPException(422, "quantity_must_be_positive")

    # A Discord ladder stop is released through the ladder FIRST, which drops
    # the guard's link to it. The helper below cancels every resting order and
    # commits; if the ladder's stop were cancelled there while the guard still
    # pointed at it, a stop-reconciler tick in the gap before the close would
    # read it as removed by the trader and forget the level — leaving whatever
    # the close doesn't sell without a stop. Released here, the reconciler
    # re-places a stop sized to the remainder once the close has settled.
    if pos.quantity > 0:
        from app.services import discord_stop_orders  # noqa: PLC0415

        discord_stop_orders.release_for_position(db, user, pos)
        db.commit()

    # Anything still resting on this contract reserves the position at the
    # broker, so the close would be refused. Clear it first — see the helper.
    cancelled = _cancel_working_orders_for_position(db, user, acct, adapter, pos)

    # RE-READ after cancelling. An order we just cancelled may have FILLED in
    # the moments before the cancel reached the broker — a resting stop is
    # exactly the kind that does. Sizing the close from the snapshot taken
    # BEFORE the cancel then sells a holding that is already gone, and a close
    # becomes a SHORT. Only pay for the extra read when we actually cancelled
    # something; the common case (nothing resting) is unchanged.
    if cancelled:
        positions = adapter.get_positions()
        pos = next((p for p in positions if p.broker_symbol.upper() == target), None)
        if pos is None or pos.quantity == 0:
            # The resting order closed it for us. Nothing left to sell, and
            # selling anyway is precisely how the short happened.
            raise HTTPException(
                409,
                "position_already_closed_by_a_resting_order",
            )

    # Reverse the side based on the current holding (long → sell, short → buy).
    reverse_side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
    full_qty = abs(pos.quantity)
    close_qty = payload.quantity if payload.quantity is not None else full_qty
    if close_qty <= 0:
        raise HTTPException(422, "quantity_must_be_positive")
    if close_qty > full_qty:
        # CLAMP, never reject. The size can legitimately shrink between the
        # client's view and now (a partial fill on the stop we just cancelled,
        # a trim elsewhere), and the user asked to get OUT — closing what is
        # actually there is the right answer. Erroring would leave them holding
        # it; over-selling would open a short.
        log.info(
            "close: clamping %s from %s to the %s actually held on %s",
            broker_symbol, close_qty, full_qty, acct.id,
        )
        close_qty = full_qty

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
        order = _place_trader_order(
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
            order = _place_trader_order(
                db, user, retry_payload, acct.id, background, request, resolve_wash_trade=True,
            )
        else:
            raise

    # Feed this exit into the re-entry basket (snapshot) so it's re-enterable —
    # same as Exit All. Record the CLOSED quantity (partial closes included).
    closed_item = _snapshot_item(pos)
    closed_item["quantity"] = str(close_qty if pos.quantity > 0 else -close_qty)
    # Single close joins today's current snapshot (don't fragment into one
    # snapshot per order). Exit-All still starts its own.
    _capture_exit_snapshot(db, user.id, [closed_item], new_event=False)
    db.commit()
    return order


@router.post("/{broker_symbol}/stop")
def set_position_stop(
    broker_symbol: str,
    request: Request,
    broker_account_id: uuid.UUID = Query(..., description="Broker account holding the position"),
    pnl_pct: Decimal = Query(..., ge=-99, le=1000, description="P&L level for the stop: -25 is 25% below entry, 0 break-even, +25 locks in 25%."),
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> dict:
    """Put the position's stop at a P&L level measured from its average price.

    Sets the level on the position's ladder guard (creating one, as the
    trailing-stop action does, if the position has none). The stop reconciler
    then keeps a real STOP order resting at that level — one mechanism for
    every stop on the row, so a ladder stop and a second bracket stop never end
    up competing for the same contracts.
    """
    from decimal import ROUND_DOWN  # noqa: PLC0415

    from app.models.discord_position_guard import DiscordPositionGuard  # noqa: PLC0415
    from app.services import audit  # noqa: PLC0415
    from app.services import discord_position_guard as guards  # noqa: PLC0415

    acct = db.get(BrokerAccount, broker_account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "broker_account_not_found")
    positions = adapter_for(acct, decrypt_json(acct.encrypted_credentials)).get_positions()
    pos = next((p for p in positions if p.broker_symbol.upper() == broker_symbol.upper()), None)
    if pos is None or pos.quantity == 0:
        raise HTTPException(404, "position_not_found")
    if pos.quantity < 0:
        # The stop reconciler places SELL stops; a short needs a BUY stop.
        raise HTTPException(422, "Stops from this row are for long positions only.")

    guard = guards.find(db, user.id, pos.symbol, pos.option_strike, pos.option_right, pos.option_expiry)
    # Measured from the position's AVERAGE price — the broker's, the "Avg entry"
    # the row shows and the price the level was previewed against. The ladder's
    # own entry is the OPENING order's price and stays put when the position is
    # added to, so after an add the two differ: a 0% stop asked for at the
    # average landed at the opening price instead (QA 2026-10-02). The ladder's
    # entry is only the fallback, for a broker that reports no average.
    avg = getattr(pos, "avg_entry_price", None)
    entry = avg if avg is not None and Decimal(str(avg)) > 0 else (
        guard.entry_price if guard is not None else None)
    if entry is None or Decimal(str(entry)) <= 0:
        raise HTTPException(422, "No entry price to measure the stop from.")
    entry = Decimal(str(entry))

    # Rounded DOWN to the cent: brokers refuse sub-cent option stops, and down
    # never tightens a stop past the level asked for.
    price = (entry * (Decimal(1) + pnl_pct / Decimal(100))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if price <= 0:
        raise HTTPException(422, "That level rounds to a $0 stop.")
    mark = getattr(pos, "current_price", None)
    if mark is not None and Decimal(str(mark)) > 0 and price >= Decimal(str(mark)):
        # A sell stop at or above the market fires at once; Alpaca refuses it.
        raise HTTPException(
            422, f"A stop at {price} is at or above the current price {mark} — it would sell immediately.",
        )

    if guard is None:
        guard = DiscordPositionGuard(
            user_id=user.id,
            symbol=pos.symbol.upper(),
            option_strike=pos.option_strike,
            option_right=(pos.option_right.value if pos.option_right else None),
            option_expiry=pos.option_expiry,
            entry_price=entry,
            sell_count=0,
        )
        db.add(guard)
    previous = guard.stop_price
    guard.stop_price = price
    audit.record(
        db, actor_user_id=user.id, action="positions.stop_set",
        entity_type="discord_position_guard", entity_id=getattr(guard, "id", None),
        metadata={"broker_symbol": pos.broker_symbol, "pnl_pct": str(pnl_pct),
                  "stop_price": str(price), "previous": str(previous) if previous is not None else None},
        ip_address=client_ip(request),
    )
    db.commit()
    log.info("positions: stop on %s set to %s (%s%% P&L)", pos.broker_symbol, price, pnl_pct)
    return {"stop_price": str(price), "entry_price": str(entry), "pnl_pct": str(pnl_pct)}


def _resting_stop_orders(db: Session, user_id, pos) -> list:
    """The trader's working STOP / TRAILING_STOP sells on this contract,
    excluding bracket legs (the bracket endpoint owns those)."""
    return list(db.execute(
        select(Order).where(
            Order.user_id == user_id,
            Order.parent_order_id.is_(None),
            Order.bracket_leg.is_(None),
            Order.symbol == pos.symbol,
            Order.option_strike.is_not_distinct_from(pos.option_strike),
            Order.option_expiry.is_not_distinct_from(pos.option_expiry),
            Order.side == OrderSide.SELL,
            Order.order_type.in_((OrderType.STOP, OrderType.TRAILING_STOP)),
            Order.status.in_((OrderStatus.PENDING, OrderStatus.SUBMITTED,
                              OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)),
        )
    ).scalars())


@router.post("/{broker_symbol}/stops/cancel")
def cancel_position_stops(
    broker_symbol: str,
    request: Request,
    broker_account_id: uuid.UUID = Query(..., description="Broker account holding the position"),
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> dict:
    """Remove every stop on one position, and keep them removed.

    * the ladder's stop — the resting order AND its level. Cancelling only the
      order left the level behind, and the stop reconciler put the order
      straight back on its next tick, even with Auto trim off;
    * a trailing exit — the app-monitored trail (options) is disarmed;
    * any other resting STOP / TRAILING_STOP sell on the contract (a native
      trailing stop on a stock).

    A bracket's SL leg is not touched here: the row clears that through the
    bracket endpoint, which keeps the entry's bracket state consistent.
    """
    from app.api.discord_sources import _cancel_stop_order  # noqa: PLC0415
    from app.services import audit, discord_stop_orders  # noqa: PLC0415
    from app.services import discord_position_guard as guards  # noqa: PLC0415

    acct = db.get(BrokerAccount, broker_account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "broker_account_not_found")
    positions = adapter_for(acct, decrypt_json(acct.encrypted_credentials)).get_positions()
    pos = next((p for p in positions if p.broker_symbol.upper() == broker_symbol.upper()), None)
    if pos is None:
        raise HTTPException(404, "position_not_found")

    cancel = _cancel_stop_order(db, user)
    removed: list[str] = []
    guard = guards.find(db, user.id, pos.symbol, pos.option_strike,
                        pos.option_right, pos.option_expiry)
    if guard is not None:
        if guard.stop_price is not None:
            removed.append(f"stop @ {guard.stop_price}")
            discord_stop_orders.release(db, guard, cancel)
            guard.stop_price = None
        if guard.trail_qty is not None:
            removed.append(f"trailing exit on {guard.trail_qty}")
            guards.clear_trail(guard)
            guard.trail_percent = None

    others = _resting_stop_orders(db, user.id, pos)
    for order in others:
        cancel(order.id)
        removed.append(f"{order.order_type.value} order")

    if not removed:
        raise HTTPException(404, "no_stops")
    audit.record(
        db, actor_user_id=user.id, action="positions.stops_cancelled",
        entity_type="position", entity_id=None,
        metadata={"broker_symbol": pos.broker_symbol, "removed": removed},
        ip_address=client_ip(request),
    )
    db.commit()
    log.warning("positions: trader removed stops on %s: %s", pos.broker_symbol, ", ".join(removed))
    return {"removed": removed}


@router.post("/{broker_symbol}/average", response_model=OrderOut)
def average_position(
    broker_symbol: str,
    payload: AveragePositionIn,
    request: Request,
    background: BackgroundTasks,
    broker_account_id: uuid.UUID = Query(..., description="Broker account holding the position"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Order:
    """Add ``quantity`` more of a held position — the mirror of close_position.

    At market, an option goes out as a LIMIT through the ask (brokers refuse
    option market orders — see _option_close_limit), exactly as a close is
    priced through the bid. It is an ordinary buy on the trader's order path,
    so subscribers copy it like any other entry.

    A Discord ladder on the contract follows the new cost basis only when this
    averages DOWN: guards.average_in, the same rule a Discord "double up" uses.
    Averaging UP leaves the entry where it was, so an add can never raise the
    ladder's own stop. The resting stop re-sizes to the new quantity on its own.
    """
    from app.services import discord_position_guard as guards  # noqa: PLC0415

    acct = db.get(BrokerAccount, broker_account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "broker_account_not_found")
    if acct.connection_status != "connected":
        raise HTTPException(409, "broker_not_connected")

    adapter = adapter_for(acct, decrypt_json(acct.encrypted_credentials))
    pos = next((p for p in adapter.get_positions()
                if p.broker_symbol.upper() == broker_symbol.upper()), None)
    if pos is None or pos.quantity == 0:
        raise HTTPException(404, "position_not_found")
    if pos.quantity < 0:
        raise HTTPException(422, "Averaging is for long positions — adding to a short would sell to open.")

    order_type, limit = payload.order_type, payload.limit_price
    if pos.instrument_type == InstrumentType.OPTION and order_type == OrderType.MARKET:
        order_type, limit = OrderType.LIMIT, _option_close_limit(adapter, pos, OrderSide.BUY)

    held = abs(Decimal(str(pos.quantity)))
    is_option = pos.instrument_type == InstrumentType.OPTION
    order = _place_trader_order(
        db, user,
        PlaceOrderIn(
            instrument_type=pos.instrument_type,
            symbol=pos.symbol,
            side=OrderSide.BUY,
            order_type=order_type,
            quantity=payload.quantity,
            limit_price=limit,
            option_expiry=pos.option_expiry if is_option else None,
            option_strike=pos.option_strike if is_option else None,
            option_right=pos.option_right if is_option else None,
        ),
        acct.id, background, request,
    )

    guard = guards.find(db, user.id, pos.symbol, pos.option_strike, pos.option_right, pos.option_expiry)
    added_price = limit if limit is not None else getattr(pos, "current_price", None)
    if (guard is not None and guard.entry_price is not None and added_price is not None
            and Decimal(str(added_price)) < guard.entry_price):
        guards.average_in(db, guard, held_qty=held, added_qty=payload.quantity,
                          added_price=Decimal(str(added_price)))
    db.commit()
    return order


@router.post("/{broker_symbol}/cancel-open")
def cancel_position_open_orders(
    broker_symbol: str,
    request: Request,
    background: BackgroundTasks,
    broker_account_id: uuid.UUID = Query(..., description="Broker account holding the position"),
    include_subscribers: bool = Query(True, description="Also cancel subscribers' mirrors of these orders."),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Cancel the open orders on THIS position's contract only.

    The row's "Canc.Open Ord" used to call cancel-all-open, which swept every
    open order on the account. This selects just the orders on the row's
    contract and account, then cancels them through the same batch path
    (broker cancel, SSE, and the cascade to subscribers' mirrors).

    A Discord ladder stop on the contract is one of those orders; its level is
    cleared too, so the ladder doesn't put it back.
    """
    from app.api.trades import _CANCELLABLE_STATUSES, _cancel_orders_batch  # noqa: PLC0415
    from app.services import discord_position_guard as guards  # noqa: PLC0415

    acct = db.get(BrokerAccount, broker_account_id)
    if not acct or acct.user_id != user.id:
        raise HTTPException(404, "broker_account_not_found")
    positions = adapter_for(acct, decrypt_json(acct.encrypted_credentials)).get_positions()
    pos = next((p for p in positions if p.broker_symbol.upper() == broker_symbol.upper()), None)
    if pos is None:
        raise HTTPException(404, "position_not_found")

    orders = _position_open_orders(db, user.id, acct.id, pos, _CANCELLABLE_STATUSES)
    result = _cancel_orders_batch(
        db, request, background, user, orders, include_subscribers, via="cancel-position-open",
    )

    guard = guards.find(db, user.id, pos.symbol, pos.option_strike, pos.option_right, pos.option_expiry)
    if guard is not None and guard.stop_order_id in {o.id for o in orders}:
        guard.stop_order_id = None
        guard.stop_price = None
        db.commit()
    return result


def _position_open_orders(db: Session, user_id, acct_id, pos, statuses) -> list:
    """The caller's open orders on one contract of one account."""
    from sqlalchemy.orm import selectinload  # noqa: PLC0415

    return list(db.execute(
        select(Order).options(selectinload(Order.fills)).where(
            Order.user_id == user_id,
            Order.broker_account_id == acct_id,
            Order.instrument_type == pos.instrument_type,
            Order.symbol == pos.symbol,
            Order.option_expiry.is_not_distinct_from(pos.option_expiry),
            Order.option_strike.is_not_distinct_from(pos.option_strike),
            Order.option_right.is_not_distinct_from(pos.option_right),
            Order.status.in_(statuses),
        )
    ).scalars())


@router.post("/{broker_symbol}/trailing-stop")
def arm_trailing_stop(
    broker_symbol: str,
    request: Request,
    background: BackgroundTasks,
    broker_account_id: uuid.UUID = Query(..., description="Broker account holding the position"),
    trail_percent: Decimal = Query(..., gt=0, le=100, description="Trailing give-back % off the current market price."),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Arm a trailing stop on one position, measured off the CURRENT market price.

    Stocks on trailing-capable brokers get a NATIVE trailing-stop order. Options
    (which brokers reject for trailing stops) get an EMULATED trail — a guard the
    P&L poller advances against the live mark and MARKET-closes when the price
    retraces ``trail_percent`` % from its peak (same engine as Discord trims)."""
    from app.models.discord_position_guard import DiscordPositionGuard  # noqa: PLC0415
    from app.services import discord_position_guard as guards  # noqa: PLC0415

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

    raw_price = pos.current_price
    if raw_price is None or Decimal(str(raw_price)) <= 0:
        raise HTTPException(422, "no_live_price_to_anchor_trail")
    price = Decimal(str(raw_price))
    held = abs(Decimal(str(pos.quantity)))
    reverse_side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY

    # Native trailing stop where the broker holds it (stocks on Alpaca). Trader-
    # only (skip_fanout) — arming a protective stop isn't a copy signal.
    if trailing_stop_close.trailing_stop_supported(adapter, pos):
        payload = PlaceOrderIn(
            instrument_type=pos.instrument_type,
            symbol=pos.symbol,
            side=reverse_side,
            order_type=OrderType.TRAILING_STOP,
            quantity=held,
            trail_percent=trail_percent,
        )
        order = _place_trader_order(
            db, user, payload, acct.id, background, request,
            skip_fanout=True, resolve_wash_trade=True,
        )
        db.commit()
        return {"mode": "native", "order_id": str(order.id), "trail_percent": str(trail_percent)}

    # Emulated trail (options): arm/refresh a guard the P&L poller enforces. The
    # dollar give-back is trail_percent % of the current mark; the peak seeds at
    # the current price and ratchets up from there.
    guard = guards.find(db, user.id, pos.symbol, pos.option_strike, pos.option_right, pos.option_expiry)
    if guard is None:
        guard = DiscordPositionGuard(
            user_id=user.id,
            symbol=pos.symbol.upper(),
            option_strike=pos.option_strike,
            option_right=(pos.option_right.value if pos.option_right else None),
            option_expiry=pos.option_expiry,
            entry_price=getattr(pos, "avg_entry_price", None),
        )
        db.add(guard)
    guard.trail_percent = trail_percent
    guards.arm_trail(guard, held, (price * trail_percent / Decimal(100)), price)
    db.commit()
    return {"mode": "emulated", "trail_percent": str(trail_percent), "peak": str(price)}
