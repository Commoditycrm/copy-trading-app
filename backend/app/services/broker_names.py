"""Resolve a SnapTrade account's underlying brokerage name.

SnapTrade-routed accounts store the real broker (Webull / Robinhood / IBKR /
…) in ``BrokerAccount.brokerage_name``, denormalized at connect time. Older
rows have it NULL — pre-migration accounts, decrypt failures during the
33ee68c24d53 backfill, or accounts connected before we wrote the name — and
those render as the generic "snaptrade".

Both the trader performance endpoint and the admin performance endpoint call
this on read so a not-yet-backfilled row shows the same label in both views.
It used to live inline in the trader endpoint only, which is exactly why the
admin table showed "snaptrade" where the trader saw "Webull (ST)".
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models.broker_account import BrokerAccount, BrokerName

log = logging.getLogger(__name__)


def _pluck(obj: Any, *names: str) -> Any:
    """Tolerant getter — the SnapTrade SDK returns mixed dict/typed objects."""
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return None


def heal_snaptrade_brokerage_names(
    db: Session, accounts: Iterable[BrokerAccount]
) -> None:
    """Backfill NULL ``brokerage_name`` on SnapTrade accounts, then persist.

    Recovery order, cheapest first:
      1. ``creds["brokerage_name"]``  — direct hit (most accounts).
      2. ``creds["brokerage_slug"]``  — older creds that stored only the slug
         ("ROBINHOOD" → "Robinhood").
      3. SnapTrade ``list_authorizations`` — last resort for legacy rows with
         neither. One network round-trip per affected account, but only until
         the column is persisted below.

    Best-effort: any per-account failure leaves the column NULL (renders
    "snaptrade" until the next call). Commits once if anything healed, so
    subsequent reads — in either endpoint — are cheap and consistent.
    """
    needs_heal = [
        a for a in accounts
        if a.broker == BrokerName.SNAPTRADE and not a.brokerage_name
    ]
    if not needs_heal:
        return

    # Lazy imports keep this off the module-load import path (crypto + the
    # SnapTrade SDK), avoiding a cycle through app.brokers.
    from app.brokers import snaptrade as snap_module  # noqa: PLC0415
    from app.services.crypto import decrypt_json  # noqa: PLC0415

    healed = False
    for a in needs_heal:
        try:
            creds = decrypt_json(a.encrypted_credentials)
        except Exception:  # noqa: BLE001
            continue
        name = (creds.get("brokerage_name") or "").strip()
        if not name:
            slug = (creds.get("brokerage_slug") or "").strip()
            if slug:
                name = slug.title()
        if not name:
            # Live lookup — match this account's authorization_id and pull the
            # brokerage's display name.
            sid = creds.get("snaptrade_user_id")
            secret = creds.get("snaptrade_user_secret")
            auth_id = creds.get("authorization_id")
            if sid and secret and auth_id:
                try:
                    auths = snap_module.list_authorizations(sid, secret)
                except Exception:  # noqa: BLE001
                    auths = []
                for auth in auths:
                    if str(_pluck(auth, "id", "authorizationId")) != str(auth_id):
                        continue
                    brokerage = _pluck(auth, "brokerage") or {}
                    cand = _pluck(brokerage, "name") or _pluck(brokerage, "slug")
                    if cand:
                        name = str(cand).strip()
                        if name.isupper():
                            name = name.title()
                    break
        # Don't persist the generic "snaptrade" — that's the fallback anyway.
        if name and name.lower() != "snaptrade":
            a.brokerage_name = name
            healed = True
    if healed:
        db.commit()
