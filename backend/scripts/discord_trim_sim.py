#!/usr/bin/env python
"""Drive the Discord trim ladder against prices you choose, with no broker.

This runs the REAL planner and the REAL enforcer — the same code the API and the
poller call — against an in-memory guard and a fake position. Nothing is sent
anywhere, so a whole ladder that would take a day of market movement runs in a
second, and you can replay the exact price path you want instead of the one the
market happens to give you.

    scripts/discord_trim_sim.py --entry 2.00 --qty 4 \
        --script "alert@3.00 alert@3.20 tick@3.10 tick@2.94 alert@3.10 tick@2.80"

  alert@PRICE   an exit alert arriving with the contract at PRICE
  tick@PRICE    one poller tick with the contract at PRICE

Settings default to the shipped ladder; --gate/--stop/--threshold/--trail
override them, or --user EMAIL loads that trader's real ones.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine                       # noqa: E402
from sqlalchemy.orm import Session                         # noqa: E402

import app.services.discord_position_guard as guards       # noqa: E402
import app.services.discord_trailing_stop as stops         # noqa: E402
from app.models.discord_position_guard import DiscordPositionGuard   # noqa: E402
from app.models.order import InstrumentType, OptionRight   # noqa: E402

DIM, BOLD, GREEN, YELLOW, RED, RESET = (
    "\033[2m", "\033[1m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
)


class _Pos:
    def __init__(self, qty, price, symbol):
        self.symbol = symbol
        self.option_strike = Decimal("100")
        self.option_right = OptionRight.CALL
        self.option_expiry = None
        self.quantity = Decimal(qty)
        self.current_price = Decimal(price)
        self.instrument_type = InstrumentType.OPTION


class _Adapter:
    def __init__(self, pos): self._p = [pos]
    def get_positions(self): return self._p


def _settings_for(email: str | None, args) -> guards.TrimConfig:
    cfg = guards.TrimConfig(
        profit_gate_pct=Decimal(str(args.gate)),
        stop_pct=Decimal(str(args.stop)),
        price_threshold=Decimal(str(args.threshold)),
        trail_amount=Decimal(str(args.trail)),
    )
    if not email:
        return cfg
    from app.database import SessionLocal
    from app.models.settings import TraderSettings
    from app.models.user import User
    from sqlalchemy import select
    with SessionLocal() as db:
        u = db.execute(select(User).where(User.email == email)).scalars().first()
        if u is None:
            sys.exit(f"no user with email {email}")
        ts = db.get(TraderSettings, u.id)
        if ts is None:
            print(f"{YELLOW}{email} has no settings row — using defaults{RESET}")
            return cfg
        return guards.TrimConfig(
            profit_gate_pct=ts.discord_trim_profit_gate_pct,
            stop_pct=ts.discord_trim_stop_pct,
            price_threshold=ts.discord_trim_price_threshold,
            trail_amount=ts.discord_trim_trail_amount,
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entry", required=True, help="fill price of the opening buy")
    ap.add_argument("--qty", required=True, type=int, help="contracts held")
    ap.add_argument("--script", required=True, help="alert@PRICE / tick@PRICE, space separated")
    ap.add_argument("--symbol", default="MSFT")
    ap.add_argument("--user", help="load this trader's real ladder settings")
    ap.add_argument("--gate", default="20"); ap.add_argument("--stop", default="25")
    ap.add_argument("--threshold", default="0.90"); ap.add_argument("--trail", default="0.25")
    args = ap.parse_args()

    cfg = _settings_for(args.user, args)
    entry, held = Decimal(args.entry), Decimal(args.qty)

    eng = create_engine("sqlite:///:memory:")
    DiscordPositionGuard.__table__.create(eng)
    db = Session(eng)
    user_id = uuid.uuid4()
    guard = DiscordPositionGuard(
        user_id=user_id, symbol=args.symbol, option_strike=Decimal("100"),
        option_right=OptionRight.CALL.value, option_expiry=None,
        sell_count=0, entry_price=entry,
    )
    db.add(guard); db.flush()

    print(f"\n{BOLD}{args.symbol}{RESET}  bought {held} @ {entry}")
    print(f"{DIM}gate +{cfg.profit_gate_pct}%  ·  1st stop -{cfg.stop_pct}%  ·  "
          f"trail above ${cfg.price_threshold}  ·  give-back ${cfg.trail_amount}{RESET}\n")

    def state():
        riding = f"  {DIM}({guard.trail_qty} riding a trail){RESET}" if guard.trail_qty else ""
        stop = guard.stop_price if guard.stop_price is not None else "—"
        return f"    held {held}  ·  stop {stop}{riding}"

    for step in args.script.split():
        kind, _, raw = step.partition("@")
        if not raw:
            sys.exit(f"bad step {step!r} — expected alert@PRICE or tick@PRICE")
        mark = Decimal(raw)

        if kind == "alert":
            plan = guards.plan_exit(guard, held, mark, cfg)
            if plan.new_stop_price is not None:
                guard.stop_price = plan.new_stop_price
            colour = GREEN if plan.sell_qty > 0 else YELLOW
            print(f"{BOLD}ALERT{RESET} @ {mark}   {colour}rung {plan.rung}: {plan.note}{RESET}")
            if plan.exit_style == guards.TRAIL and plan.sell_qty > 0:
                guards.arm_trail(guard, plan.sell_qty, plan.trail_amount, mark)
            elif plan.sell_qty > 0:
                held -= plan.sell_qty
                print(f"    {GREEN}sold {plan.sell_qty} at market{RESET}")
            if plan.retire:
                guards.retire(db, guard, plan.note)

        elif kind == "tick":
            out = []
            stops.enforce(db, user_id, _Adapter(_Pos(held, mark, args.symbol)),
                          lambda p, g, q: out.append(Decimal(str(q))))
            if out:
                held -= out[0]
                why = guard.closed_reason or "trailing exit"
                print(f"{BOLD}TICK {RESET} @ {mark}   {RED}sold {out[0]} — {why}{RESET}")
            else:
                print(f"{DIM}TICK  @ {mark}   nothing triggered{RESET}")
        else:
            sys.exit(f"unknown step {kind!r} — use alert@ or tick@")

        print(state())
        if guard.closed_at:
            print(f"\n{BOLD}position closed{RESET} — {guard.closed_reason}\n")
            return

    print(f"\n{BOLD}still open{RESET}: {held} held, stop "
          f"{guard.stop_price if guard.stop_price is not None else '—'}\n")


if __name__ == "__main__":
    main()
