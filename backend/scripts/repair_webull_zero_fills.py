"""Repair Webull orders left at FILLED with filled_quantity = 0.

Why these exist
---------------
Webull spells the filled quantity differently per endpoint:

    /openapi/trade/order/detail   orders[].filled_quantity
    Query Day Orders              orders[].items[].filled_qty

Only the second was handled, so a detail read of a filled order wrote
status=FILLED with filled_quantity=0 (fixed in the adapter; see its
_fetch_detail comment). This script repairs the rows written before that.

Why they do not fix themselves
------------------------------
``fills_sync._refresh_open_orders`` only re-reads NON-TERMINAL orders, and FILLED
is terminal. So an affected row is stuck permanently — and it is not cosmetic:
``copy_engine._closeable_quantity`` sums filled_quantity, so the subscriber reads
as FLAT while actually holding the position. Their next mirror SELL goes out as
SELL_TO_OPEN and Webull refuses it with
``OPENAPI_POSITION_ORDER_INTENT_MISMATCH``.

Usage
-----
    .venv/bin/python scripts/repair_webull_zero_fills.py            # dry run
    .venv/bin/python scripts/repair_webull_zero_fills.py --apply    # write

Dry run by default: it prints exactly what it would change and touches nothing.
Each order is re-read from Webull individually, so this spends API budget
(~10 requests / 30s per app_key) — it paces itself and is safe to re-run.

Only ever writes filled_quantity / filled_avg_price, and only when the BROKER
reports a larger fill than we have. It never invents a fill, never lowers one,
and never changes status.
"""
from __future__ import annotations

import argparse
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from app.brokers import adapter_for
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.order import Order, OrderStatus
from app.services.crypto import decrypt_json

# Webull's trade endpoints share ~10 requests / 30s per app_key. One read per
# order, spaced, so a large backlog cannot throttle the live app alongside it.
_PACE_S = 3.5


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the repairs (default: dry run)")
    ap.add_argument("--limit", type=int, default=200,
                    help="max orders to examine (default 200)")
    args = ap.parse_args()

    with SessionLocal() as db:
        rows = db.execute(
            select(Order.id, Order.broker_account_id)
            .join(BrokerAccount, BrokerAccount.id == Order.broker_account_id)
            .where(
                BrokerAccount.broker == BrokerName.WEBULL,
                Order.status == OrderStatus.FILLED,
                func.coalesce(Order.filled_quantity, 0) == 0,
                Order.broker_order_id.is_not(None),
            )
            .order_by(Order.created_at.desc())
            .limit(args.limit)
        ).all()

    if not rows:
        print("nothing to repair — no Webull orders at FILLED with filled_quantity=0")
        return 0

    print(f"{len(rows)} candidate order(s){'' if args.apply else '  (DRY RUN — nothing will be written)'}")
    adapters: dict = {}
    repaired = unchanged = failed = 0

    for i, (order_id, acct_id) in enumerate(rows):
        if i:
            time.sleep(_PACE_S)
        with SessionLocal() as db:
            order = db.get(Order, order_id)
            acct = db.get(BrokerAccount, acct_id)
            if order is None or acct is None:
                continue
            try:
                if acct_id not in adapters:
                    adapters[acct_id] = adapter_for(
                        acct, decrypt_json(acct.encrypted_credentials)
                    )
                res = adapters[acct_id].get_order(order.broker_order_id)
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL  {order.symbol:<6} {order.broker_order_id}  {str(exc)[:90]}")
                failed += 1
                continue

            broker_qty = Decimal(str(res.filled_quantity or 0))
            our_qty = Decimal(str(order.filled_quantity or 0))
            if broker_qty <= our_qty:
                print(f"  same  {order.symbol:<6} broker={broker_qty} ours={our_qty}")
                unchanged += 1
                continue

            print(f"  FIX   {order.symbol:<6} {order.side.value:<4} "
                  f"strike={order.option_strike} filled {our_qty} -> {broker_qty} "
                  f"@ {res.filled_avg_price}")
            if args.apply:
                order.filled_quantity = broker_qty
                if res.filled_avg_price is not None and not order.filled_avg_price:
                    order.filled_avg_price = res.filled_avg_price
                db.commit()
            repaired += 1

    verb = "repaired" if args.apply else "would repair"
    print(f"\n{verb}: {repaired} | already correct: {unchanged} | failed: {failed}")
    if repaired and not args.apply:
        print("re-run with --apply to write these changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
