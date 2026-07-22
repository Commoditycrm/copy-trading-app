"""Position reconciler — compares our order-derived net position against the
broker's ACTUAL holdings, per subscriber account.

Why this exists
---------------
Subscriber P&L (the Calendar / realized-P&L view) is computed by FIFO over the
`fills` of a subscriber's orders. That history drifts from reality because we
MISS broker-side closes: a SnapTrade→Webull subscriber closes a position in
their broker app, our poller doesn't catch the close inside its window, and our
records keep the lot "open" forever. Over time the order-derived net for a
symbol diverges wildly from what the subscriber actually holds (observed on
prod: our records showed ~370 contracts across 15 symbols; the broker held 5
across 4). Every P&L number computed from that history is then wrong.

Note this does NOT affect the position CARDS — those call get_positions() live
(see api/positions.py), so they already show the truth. It's the ORDER/FILL
history, and everything derived from it (realized P&L, trade counts, history),
that drifts.

The existing SnapTrade fill reconciler syncs *recent activity* but never checks
`get_positions()`, so it can't see accumulated drift. This module closes that
gap: order-derived net vs get_positions(), per contract.

Dry-run first
-------------
This module currently only REPORTS divergences (dry_run=True). The correction
path (recording the missing closes so the derived net matches the broker) is a
separate, audited step — see reconcile_account's ``dry_run=False`` stub. We ship
the reporter first so we can measure the blast radius across all subscribers
before any money-adjacent write.
"""
from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.brokers import adapter_for
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import (
    InstrumentType, Order, OrderSide, OrderStatus, OrderType, OptionRight,
)
from app.models.user import User, UserRole
from app.services import audit
from app.services.crypto import decrypt_json

# Marks synthetic reconciliation rows in broker_order_id. Every row this module
# writes carries it, so they're queryable and a bad run is undone by deleting
# tagged rows — there's no broker side effect to unwind.
RECONCILE_TAG = "RECONCILE:"

log = logging.getLogger(__name__)


# A single tradeable instrument. Stocks key on symbol alone; options need the
# full contract (expiry/strike/right) so AAPL stock and an AAPL call are never
# conflated. Mirrors the same-contract key used by copy_engine._closeable_quantity.
@dataclass(frozen=True)
class ContractKey:
    symbol: str
    instrument_type: InstrumentType
    option_expiry: date | None = None
    option_strike: Decimal | None = None
    option_right: OptionRight | None = None

    def label(self) -> str:
        if self.instrument_type != InstrumentType.OPTION:
            return self.symbol
        r = self.option_right.value.upper()[0] if self.option_right else "?"
        # Normalise the strike for display so "330.0000" and "330.0" — which are
        # already the SAME key (Decimal equality) — don't render two ways. Trim
        # trailing zeros without going to scientific notation.
        strike = self.option_strike
        if strike is not None:
            s = format(strike.normalize(), "f")
        else:
            s = "?"
        return f"{self.symbol} {s}{r} {self.option_expiry}"


class DivergenceClass(str, enum.Enum):
    """How the write path should treat a divergence. Only AUTO is safe to
    correct automatically — see the write-path scope. Everything else is
    surfaced for a human because we can't derive its realized P&L on our own."""
    # Option past expiry, broker flat for the contract, no offsetting share
    # position → expired worthless. Close price is unambiguously 0. Auto-fixable.
    AUTO_EXPIRED_WORTHLESS = "auto_expired_worthless"
    # Option past expiry, broker flat, BUT an offsetting share position exists →
    # likely exercised/assigned. Settles into stock at the strike, not 0.
    FLAG_ASSIGNMENT = "flag_assignment"
    # Anything else: a still-tradeable instrument whose net is wrong (missed
    # close / phantom fill on a live symbol, e.g. CPHI). Correct price unknowable
    # from our side — needs the broker's trade history.
    FLAG_LIVE_DRIFT = "flag_live_drift"


def _has_offsetting_share(
    contract: ContractKey, broker_positions: dict[ContractKey, Decimal]
) -> bool:
    """True when the broker holds a non-zero STOCK position in the option's
    underlying — the fingerprint of an exercise/assignment settling into shares.
    Conservative: any offsetting stock blocks the worthless assumption."""
    for key, qty in broker_positions.items():
        if (
            key.instrument_type == InstrumentType.STOCK
            and key.symbol == contract.symbol
            and qty != 0
        ):
            return True
    return False


def classify_divergence(
    contract: ContractKey,
    our_net: Decimal,
    broker_net: Decimal,
    broker_positions: dict[ContractKey, Decimal],
    today: date,
) -> DivergenceClass:
    """Label a divergence for the write path. Deliberately narrow: AUTO only for
    the case we can price with certainty (expired worthless option); everything
    else flags."""
    if (
        contract.instrument_type == InstrumentType.OPTION
        and contract.option_expiry is not None
        and contract.option_expiry < today          # genuinely past expiry
        and broker_net == 0                          # broker dropped the contract
        and our_net != 0                             # we still carry it
    ):
        if _has_offsetting_share(contract, broker_positions):
            return DivergenceClass.FLAG_ASSIGNMENT
        return DivergenceClass.AUTO_EXPIRED_WORTHLESS
    return DivergenceClass.FLAG_LIVE_DRIFT


@dataclass
class ProposedClose:
    """The synthetic closing order the write path would insert for an
    auto-fixable divergence. Populated for AUTO divergences even in dry-run, so
    a caller can diff exactly what an apply=True run would write."""
    side: OrderSide           # opposite of our net
    quantity: Decimal         # abs(our_net)
    price: Decimal            # 0 for expired-worthless
    parent_order_id: uuid.UUID
    reason: str
    written_order_id: uuid.UUID | None = None  # set only after an apply write


@dataclass
class Divergence:
    contract: ContractKey
    our_net: Decimal      # signed: + long, - short (filled buys - sells)
    broker_net: Decimal   # signed, from get_positions()
    classification: DivergenceClass = DivergenceClass.FLAG_LIVE_DRIFT
    proposed_close: ProposedClose | None = None
    @property
    def delta(self) -> Decimal:
        return self.our_net - self.broker_net


def _same_contract(contract: ContractKey):
    """SQLAlchemy predicate tuple matching one exact contract on Order rows."""
    return (
        Order.symbol == contract.symbol,
        Order.instrument_type == contract.instrument_type,
        Order.option_expiry.is_not_distinct_from(contract.option_expiry),
        Order.option_strike.is_not_distinct_from(contract.option_strike),
        Order.option_right.is_not_distinct_from(contract.option_right),
    )


def _representative_parent(
    db: Session, broker_account_id: uuid.UUID, contract: ContractKey
) -> uuid.UUID | None:
    """A parent_order_id from an existing mirror order on this contract, so the
    synthetic close lands in the SAME mirrors_only FIFO timeline the realized-P&L
    view walks. None if there's no mirror to attach to — then we can't safely
    write a subscriber close and must flag instead of auto-fixing."""
    return db.execute(
        select(Order.parent_order_id)
        .where(
            Order.broker_account_id == broker_account_id,
            *_same_contract(contract),
            Order.parent_order_id.is_not(None),
        )
        .limit(1)
    ).scalar_one_or_none()


def _expiry_fill_time(contract: ContractKey) -> datetime:
    """When to date the synthetic close. Use the option's expiry at the US close
    (16:00 ET → UTC) so the realized P&L lands on the day the option actually
    expired, not the day we happened to reconcile."""
    from app.services import market_hours  # noqa: PLC0415
    exp = contract.option_expiry or market_hours.now_et().date()
    et_dt = datetime.combine(exp, time(16, 0), tzinfo=market_hours.ET)
    return et_dt.astimezone(timezone.utc)


def _build_proposed_close(
    db: Session, broker_account_id: uuid.UUID, div: Divergence
) -> ProposedClose | None:
    """The synthetic close for an auto-fixable divergence, or None if we can't
    attach it to a mirror (in which case the caller downgrades to a flag)."""
    parent = _representative_parent(db, broker_account_id, div.contract)
    if parent is None:
        return None
    # Close in the direction that flattens our net: short (net<0) → BUY back;
    # long (net>0) → SELL. Quantity is the phantom amount.
    side = OrderSide.BUY if div.our_net < 0 else OrderSide.SELL
    return ProposedClose(
        side=side,
        quantity=abs(div.our_net),
        price=Decimal(0),  # expired worthless
        parent_order_id=parent,
        reason="expired worthless",
    )


def _write_close(db: Session, account: BrokerAccount, div: Divergence) -> uuid.UUID:
    """Insert the synthetic closing order + audit it. Caller owns the txn/commit.
    NEVER calls the broker — this is bookkeeping only."""
    pc = div.proposed_close
    assert pc is not None
    c = div.contract
    fill_at = _expiry_fill_time(c)
    order = Order(
        user_id=account.user_id,
        broker_account_id=account.id,
        parent_order_id=pc.parent_order_id,
        instrument_type=c.instrument_type,
        symbol=c.symbol,
        option_expiry=c.option_expiry,
        option_strike=c.option_strike,
        option_right=c.option_right,
        side=pc.side,
        order_type=OrderType.MARKET,
        quantity=pc.quantity,
        filled_quantity=pc.quantity,
        filled_avg_price=pc.price,
        status=OrderStatus.FILLED,
        is_closing=True,
        submitted_at=fill_at,
        closed_at=fill_at,
        broker_order_id=f"{RECONCILE_TAG}{uuid.uuid4()}",
        reject_reason=f"position reconcile: {pc.reason}",
    )
    db.add(order)
    db.flush()  # assign order.id for the audit + return
    pc.written_order_id = order.id
    audit.record(
        db,
        actor_user_id=account.user_id,
        action="position.reconciled",
        entity_type="order",
        entity_id=order.id,
        metadata={
            "contract": c.label(),
            "our_net": str(div.our_net),
            "broker_net": str(div.broker_net),
            "close_side": pc.side.value,
            "close_qty": str(pc.quantity),
            "close_price": str(pc.price),
            "basis": pc.reason,
            "broker_account_id": str(account.id),
        },
    )
    return order.id


@dataclass
class AccountReconcileReport:
    user_email: str
    broker_account_id: uuid.UUID
    broker: str
    divergences: list[Divergence] = field(default_factory=list)
    error: str | None = None

    @property
    def in_sync(self) -> bool:
        return self.error is None and not self.divergences


def order_derived_positions(
    db: Session, account: BrokerAccount
) -> dict[ContractKey, Decimal]:
    """Net FILLED position per contract, from our order records — the net the
    realized-P&L FIFO actually walks. Signed: filled BUY qty − filled SELL qty.

    Two things this MUST match, or the reconciler reports phantom divergences:

    1. De-dup with dedupe_subscriber_orders — the SAME dedupe realized_pnl_by_day
       uses. Counting raw fills was the false-phantom bug: the SnapTrade
       listener re-records a mirror's fill as a duplicate standalone row, so a
       closed position (mirror buy + standalone-dup sell, or vice versa) netted
       to a nonzero phantom here while the P&L FIFO had already closed it.
       (Observed on Karthik: AMZN 250C / META 640P reported as −5 / −1 open when
       they were flat.)

    2. Include reconnect-orphaned rows (broker_account_id NULL). When a broker is
       disconnected the Order.broker_account_id is SET NULL (audit-trail
       preservation), so filtering strictly by account.id dropped the closing
       legs of positions opened before a reconnect — resurrecting closed lots as
       phantoms. We scope to this user's rows on THIS account or orphaned.
    """
    from app.services.pnl import dedupe_subscriber_orders  # noqa: PLC0415

    orders_all = list(
        db.execute(
            select(Order).where(
                Order.user_id == account.user_id,
                or_(
                    Order.broker_account_id == account.id,
                    Order.broker_account_id.is_(None),
                ),
                Order.status == OrderStatus.FILLED,
                Order.filled_quantity > 0,
            )
        ).scalars()
    )

    net: dict[ContractKey, Decimal] = {}
    for o in dedupe_subscriber_orders(orders_all):
        key = ContractKey(
            o.symbol, o.instrument_type, o.option_expiry,
            o.option_strike, o.option_right,
        )
        q = Decimal(o.filled_quantity)
        net[key] = net.get(key, Decimal(0)) + (q if o.side == OrderSide.BUY else -q)
    # Drop contracts that net to flat — they're not divergences, they're closed.
    return {k: v for k, v in net.items() if v != 0}


def _broker_positions(account: BrokerAccount) -> dict[ContractKey, Decimal]:
    """Live get_positions() for the account, keyed like order_derived_positions.
    Raises on broker/credential failure (caller records it as the account's
    error rather than a silent empty)."""
    creds = decrypt_json(account.encrypted_credentials)
    adapter = adapter_for(account, creds)
    out: dict[ContractKey, Decimal] = {}
    for p in adapter.get_positions():
        key = ContractKey(
            p.symbol, p.instrument_type, p.option_expiry, p.option_strike, p.option_right
        )
        out[key] = out.get(key, Decimal(0)) + Decimal(str(p.quantity))
    return {k: v for k, v in out.items() if v != 0}


def reconcile_account(
    db: Session, account: BrokerAccount, *, apply: bool = False
) -> AccountReconcileReport:
    """Compare one account's order-derived net against the broker's actual
    holdings, classify each divergence, and attach the proposed synthetic close
    for the auto-fixable ones.

    apply=False (default): writes NOTHING. Every auto divergence still gets its
    ``proposed_close`` populated, so a caller can diff exactly what an apply run
    would do. This is the safe default.

    apply=True: writes the synthetic closes for AUTO_EXPIRED_WORTHLESS
    divergences only (flags are never written) and audits each. Caller commits.
    """
    user = db.get(User, account.user_id)
    report = AccountReconcileReport(
        user_email=user.email if user else str(account.user_id),
        broker_account_id=account.id,
        broker=account.broker.value if account.broker else "?",
    )
    try:
        ours = order_derived_positions(db, account)
        broker = _broker_positions(account)
    except Exception as exc:  # noqa: BLE001
        report.error = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("position_reconciler: %s failed: %s", account.id, report.error)
        return report

    # Empty-broker guard. SnapTrade's get_positions() SWALLOWS a stale/404
    # connection and returns [] instead of raising (observed on QA: 40/43
    # accounts came back empty because their connections had gone stale). If we
    # took "empty" to mean "flat", every open position would look divergent and
    # an apply run would try to CLOSE live positions the broker simply failed to
    # report. So: broker empty AND we hold something → treat as unreachable, not
    # flat. Skip the account rather than reconcile against a blank. The cost of a
    # false skip (a genuinely all-flat account, rare) is nil; the cost of the
    # opposite is closing real money positions. A working account (e.g. Karthik,
    # who returns real holdings) never trips this.
    if not broker and ours:
        report.error = (
            f"broker returned no positions but our records show {len(ours)} open "
            f"contract(s) — treating connection as unreachable, not flat; skipped "
            f"to avoid closing live positions"
        )
        log.warning("position_reconciler: %s empty-broker guard tripped", account.id)
        return report

    from app.services import market_hours  # noqa: PLC0415
    today = market_hours.now_et().date()  # expiry is judged in market time

    for key in sorted(set(ours) | set(broker), key=lambda k: k.label()):
        o = ours.get(key, Decimal(0))
        b = broker.get(key, Decimal(0))
        if o == b:
            continue
        div = Divergence(
            contract=key, our_net=o, broker_net=b,
            classification=classify_divergence(key, o, b, broker, today),
        )
        if div.classification == DivergenceClass.AUTO_EXPIRED_WORTHLESS:
            div.proposed_close = _build_proposed_close(db, account.id, div)
            if div.proposed_close is None:
                # No mirror to attach the close to — can't safely write a
                # subscriber close. Downgrade to a flag rather than fabricate.
                div.classification = DivergenceClass.FLAG_LIVE_DRIFT
        report.divergences.append(div)

    if apply:
        for div in report.divergences:
            if (
                div.classification == DivergenceClass.AUTO_EXPIRED_WORTHLESS
                and div.proposed_close is not None
            ):
                _write_close(db, account, div)
        db.flush()
    return report


def reconcile_all_subscribers(*, apply: bool = False) -> list[AccountReconcileReport]:
    """Run the reconciler across every connected SnapTrade subscriber account.
    SnapTrade is the drift source (polled, misses closes); direct brokers report
    fills reliably and don't accumulate this way, so they're out of scope until
    proven otherwise.

    apply=False (default) writes nothing. apply=True commits PER ACCOUNT — a
    write failure on one subscriber never half-writes another (each account is
    its own transaction boundary)."""
    from app.database import SessionLocal  # noqa: PLC0415
    out: list[AccountReconcileReport] = []
    with SessionLocal() as db:
        accounts = db.execute(
            select(BrokerAccount)
            .join(User, User.id == BrokerAccount.user_id)
            .where(
                User.role == UserRole.SUBSCRIBER,
                BrokerAccount.broker == BrokerName.SNAPTRADE,
                BrokerAccount.connection_status == "connected",
            )
        ).scalars().all()
        for acct in accounts:
            report = reconcile_account(db, acct, apply=apply)
            if apply and report.error is None:
                db.commit()   # per-account boundary
            elif apply:
                db.rollback()  # this account errored — don't carry a partial txn
            out.append(report)
    return out
