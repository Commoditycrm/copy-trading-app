"""Quick local diagnostic: dump every broker_account's auth_type so you
can verify which connections have trade-placement scope.

Run from the backend dir:
    .venv/bin/python scripts/check_auth_type.py

Output is a table of (user_id, role, broker, brokerage_name, auth_type).
For SnapTrade rows, look for ``auth_type=trade`` — that's the one that
permits mirror orders. ``read`` means SnapTrade only gave us read scope,
and any place_order call against that account would 403.

Non-SnapTrade rows (Alpaca, IBKR direct) don't carry an auth_type and
print "—" — they always have full trade permission via their API keys.
"""
from __future__ import annotations

import os
import sys

# Allow `from app.x import y` when running this script from anywhere.
_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from sqlalchemy import select

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount, BrokerName
from app.models.user import User
from app.services.crypto import decrypt_json


def main() -> None:
    with SessionLocal() as db:
        rows = db.execute(
            select(BrokerAccount, User)
            .join(User, BrokerAccount.user_id == User.id)
            .order_by(User.role, BrokerAccount.created_at)
        ).all()

        if not rows:
            print("No broker accounts.")
            return

        # Column widths tuned for readable terminal output.
        print(
            f"  {'role':<11} {'email':<32} {'broker':<10} {'brokerage':<22} "
            f"{'status':<14} {'auth_type':<10}"
        )
        print("  " + "-" * 100)

        for acct, user in rows:
            auth_type = "—"
            brokerage = "—"
            if acct.broker == BrokerName.SNAPTRADE and acct.encrypted_credentials:
                try:
                    creds = decrypt_json(acct.encrypted_credentials)
                    auth_type = str(creds.get("auth_type") or "?")
                    brokerage = str(creds.get("brokerage_name") or "?")[:22]
                except Exception as exc:  # noqa: BLE001
                    auth_type = f"decrypt-err"
                    brokerage = f"({exc.__class__.__name__})"
            elif acct.broker == BrokerName.ALPACA:
                brokerage = "Alpaca (direct)"
                auth_type = "(direct keys)"

            # Color the auth_type green/red so problems jump out.
            colored = auth_type
            if auth_type == "trade":
                colored = f"\033[32m{auth_type:<10}\033[0m"
            elif auth_type == "read":
                colored = f"\033[31m{auth_type:<10}\033[0m"
            else:
                colored = f"{auth_type:<10}"

            print(
                f"  {user.role.value:<11} {user.email[:32]:<32} "
                f"{acct.broker.value:<10} {brokerage:<22} "
                f"{acct.connection_status:<14} {colored}"
            )


if __name__ == "__main__":
    main()
