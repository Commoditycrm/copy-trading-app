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

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.brokers import adapter_for
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import InstrumentType, Order, OrderSide, OrderStatus, OptionRight
from app.models.user import User, UserRole
from app.services.crypto import decrypt_json

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


@dataclass
class Divergence:
    contract: ContractKey
    our_net: Decimal      # signed: + long, - short (filled buys - sells)
    broker_net: Decimal   # signed, from get_positions()
    @property
    def delta(self) -> Decimal:
        return self.our_net - self.broker_net


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
    db: Session, broker_account_id: uuid.UUID
) -> dict[ContractKey, Decimal]:
    """Net FILLED position per contract for one broker account, from our order
    records. Signed: filled BUY qty − filled SELL qty. This is what the derived
    P&L history believes the account holds.

    Counts ALL filled orders on the account (not mirrors-only): what actually
    hit the broker is the sum of every fill, which is exactly what get_positions
    reflects. If standalone listener duplicates inflate this, that inflation IS
    part of the drift we want the report to surface.
    """
    rows = db.execute(
        select(
            Order.symbol,
            Order.instrument_type,
            Order.option_expiry,
            Order.option_strike,
            Order.option_right,
            Order.side,
            func.coalesce(func.sum(Order.filled_quantity), 0),
        )
        .where(
            Order.broker_account_id == broker_account_id,
            Order.status == OrderStatus.FILLED,
            Order.filled_quantity > 0,
        )
        .group_by(
            Order.symbol, Order.instrument_type, Order.option_expiry,
            Order.option_strike, Order.option_right, Order.side,
        )
    ).all()

    net: dict[ContractKey, Decimal] = {}
    for symbol, itype, expiry, strike, right, side, qty in rows:
        key = ContractKey(symbol, itype, expiry, strike, right)
        signed = Decimal(str(qty)) if side == OrderSide.BUY else -Decimal(str(qty))
        net[key] = net.get(key, Decimal(0)) + signed
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
    db: Session, account: BrokerAccount, *, dry_run: bool = True
) -> AccountReconcileReport:
    """Compare one account's order-derived net against the broker's actual
    holdings and report every contract where they disagree.

    dry_run=True (the only supported mode today): writes nothing.
    dry_run=False: NOT YET IMPLEMENTED — the correction path (recording the
    missing closes, audited) is a deliberate follow-up. It raises so nobody
    accidentally mutates money data before that path is designed and reviewed.
    """
    user = db.get(User, account.user_id)
    report = AccountReconcileReport(
        user_email=user.email if user else str(account.user_id),
        broker_account_id=account.id,
        broker=account.broker.value if account.broker else "?",
    )
    try:
        ours = order_derived_positions(db, account.id)
        broker = _broker_positions(account)
    except Exception as exc:  # noqa: BLE001
        report.error = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("position_reconciler: %s failed: %s", account.id, report.error)
        return report

    for key in sorted(set(ours) | set(broker), key=lambda k: k.label()):
        o = ours.get(key, Decimal(0))
        b = broker.get(key, Decimal(0))
        if o != b:
            report.divergences.append(Divergence(contract=key, our_net=o, broker_net=b))

    if not dry_run:
        # The corrective write is intentionally not built yet. Reporting first
        # (see module docstring). When implemented it must: record a reconciling
        # adjustment so the derived net matches the broker, NEVER place a real
        # broker order, and audit every change (action="position.reconciled").
        raise NotImplementedError(
            "position_reconciler write path not implemented — dry_run only. "
            "The correction step is a separate, audited change."
        )
    return report


def reconcile_all_subscribers(*, dry_run: bool = True) -> list[AccountReconcileReport]:
    """Dry-run the reconciler across every connected SnapTrade subscriber
    account. SnapTrade is the drift source (polled, misses closes); direct
    brokers report fills reliably and don't accumulate this way, so they're out
    of scope until proven otherwise."""
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
            out.append(reconcile_account(db, acct, dry_run=dry_run))
    return out
