"""Re-run the parser over stored Discord messages.

Parses are recorded at INTAKE, so a message keeps whatever verdict the parser
gave when it arrived. That's deliberate — the audit trail should show what we
actually understood at the time — but it means a parser fix doesn't reach
messages already stored.

Run this after changing a parser to bring history in line:

    python -m scripts.reparse_discord_messages          # report only
    python -m scripts.reparse_discord_messages --apply  # write the changes
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.database import SessionLocal
from app.models.discord_message import DiscordMessage, DiscordMessageStatus
from app.services.discord_parsers import ParsedMessage, ParseStatus, parse_message

_STATUS = {
    ParseStatus.PARSED: DiscordMessageStatus.PARSED,
    ParseStatus.INVALID: DiscordMessageStatus.INVALID,
    ParseStatus.IGNORED: DiscordMessageStatus.IGNORED,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    changed = 0
    with SessionLocal() as db:
        rows = list(db.execute(select(DiscordMessage)).scalars())
        for row in rows:
            # Never re-parse a message that already produced an order: its
            # reading is what the order was placed on, and rewriting it would
            # falsify the audit trail.
            if row.status in (
                DiscordMessageStatus.ORDER_CREATED, DiscordMessageStatus.ORDER_FAILED
            ):
                continue

            result = parse_message(
                ParsedMessage(
                    content=row.content or "",
                    embeds=list(row.embeds or []),
                    author=row.author,
                    posted_at=row.posted_at,
                )
            )
            new_status = _STATUS[result.status]
            new_signals = [s.as_dict() for s in result.signals]
            if new_status == row.status and new_signals == list(row.parsed_signals or []):
                continue

            changed += 1
            label = (row.embeds or [{}])[0].get("title") or (row.content or "")
            print(f"  {row.status.value:>8} -> {new_status.value:<8} {label[:60]!r}")
            if args.apply:
                row.status = new_status
                row.parsed_signals = new_signals
                row.parsed_signal = new_signals[0] if new_signals else None
                row.status_reason = (
                    (f"{len(new_signals)} trades in this alert" if len(new_signals) > 1 else None)
                    if result.status is ParseStatus.PARSED
                    else (result.reason or "")[:480]
                )
        if args.apply:
            db.commit()

    print(f"\n{changed} of {len(rows)} message(s) would change" if not args.apply
          else f"\n{changed} of {len(rows)} message(s) updated")


if __name__ == "__main__":
    main()
