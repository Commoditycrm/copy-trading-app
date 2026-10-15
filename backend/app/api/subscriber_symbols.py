"""Per-symbol copy settings a subscriber manages themselves.

One rule per symbol, holding only what that symbol overrides. Everything a rule
does not mention falls through to the subscriber's own settings, so a rule is
always a difference rather than a replacement — see
services/subscriber_symbol_settings.

Each response carries ``overridden``: which fields this symbol actually sets.
Without it the UI could not tell a value the subscriber chose from one it
inherited, and "which symbols are customised" would be a guess.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, require_subscriber
from app.database import get_db
from app.models.settings import SubscriberSettings
from app.models.subscriber_symbol_settings import SubscriberSymbolSettings
from app.models.user import User
from app.schemas.subscriber_symbols import (
    SymbolRuleIn,
    SymbolRuleOut,
    SymbolRulesOut,
)
from app.services import audit
from app.services import subscriber_symbol_settings as symbols

router = APIRouter(prefix="/api/settings/subscriber/symbols", tags=["settings"])


def _out(rule: SubscriberSymbolSettings, base: SubscriberSettings | None) -> SymbolRuleOut:
    """A rule plus the EFFECTIVE values it produces.

    Both, deliberately: the overrides alone can't be rendered without the
    subscriber's settings to fall back on, and the effective values alone can't
    show which are custom.
    """
    ts = symbols.SymbolSettings(base, rule.settings or {})
    effective: dict[str, Any] = {}
    for field in symbols.SYMBOL_FIELDS:
        value = getattr(ts, field, None)
        effective[field] = str(value) if hasattr(value, "quantize") else value
    return SymbolRuleOut(
        symbol=rule.symbol,
        enabled=rule.enabled,
        overridden=ts.overridden,
        settings=dict(rule.settings or {}),
        effective=effective,
        exit_mode=symbols.exit_mode(ts),
    )


@router.get("", response_model=SymbolRulesOut)
def list_symbol_rules(
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SymbolRulesOut:
    """Every symbol this subscriber has customised, alphabetically."""
    base = db.get(SubscriberSettings, user.id)
    rows = db.execute(
        select(SubscriberSymbolSettings)
        .where(SubscriberSymbolSettings.user_id == user.id)
        .order_by(SubscriberSymbolSettings.symbol.asc())
    ).scalars().all()
    return SymbolRulesOut(symbols=[_out(r, base) for r in rows])


@router.get("/{symbol}", response_model=SymbolRuleOut)
def get_symbol_rule(
    symbol: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SymbolRuleOut:
    root = symbols.normalize(symbol)
    rule = db.execute(
        select(SubscriberSymbolSettings).where(
            SubscriberSymbolSettings.user_id == user.id,
            SubscriberSymbolSettings.symbol == root,
        )
    ).scalars().first()
    if rule is None:
        raise HTTPException(404, "symbol_rule_not_found")
    return _out(rule, db.get(SubscriberSettings, user.id))


@router.put("/{symbol}", response_model=SymbolRuleOut)
def upsert_symbol_rule(
    symbol: str,
    payload: SymbolRuleIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> SymbolRuleOut:
    """Create or update one symbol's rule.

    ``settings`` replaces the stored overrides wholesale rather than merging:
    the UI knows the full set of fields it is editing, and a merge would make
    CLEARING an override impossible — the field would simply be absent from the
    patch and the old value would survive.
    """
    root = symbols.normalize(symbol)
    if not root or not root.isalnum():
        raise HTTPException(422, "invalid_symbol")

    unknown = sorted(set(payload.settings or {}) - set(symbols.SYMBOL_FIELDS))
    if unknown:
        # Named rather than ignored: a typo that silently does nothing is worse
        # than a refusal, because the subscriber believes they set something.
        raise HTTPException(422, f"not_overridable: {', '.join(unknown)}")

    rule = db.execute(
        select(SubscriberSymbolSettings).where(
            SubscriberSymbolSettings.user_id == user.id,
            SubscriberSymbolSettings.symbol == root,
        )
    ).scalars().first()
    created = rule is None
    if rule is None:
        rule = SubscriberSymbolSettings(user_id=user.id, symbol=root)
        db.add(rule)
    rule.settings = dict(payload.settings or {})
    if payload.enabled is not None:
        rule.enabled = payload.enabled
    db.flush()

    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.symbol_rule_saved",
        entity_type="subscriber_symbol_settings",
        entity_id=rule.id,
        metadata={"symbol": root, "created": created,
                  "overridden": sorted(rule.settings or {})},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(rule)
    return _out(rule, db.get(SubscriberSettings, user.id))


@router.delete("/{symbol}", status_code=204, response_model=None)
def delete_symbol_rule(
    symbol: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscriber),
) -> None:
    """Drop a symbol's rule — it goes back to the subscriber's own settings."""
    root = symbols.normalize(symbol)
    rule = db.execute(
        select(SubscriberSymbolSettings).where(
            SubscriberSymbolSettings.user_id == user.id,
            SubscriberSymbolSettings.symbol == root,
        )
    ).scalars().first()
    if rule is None:
        raise HTTPException(404, "symbol_rule_not_found")
    db.delete(rule)
    audit.record(
        db,
        actor_user_id=user.id,
        action="subscriber.symbol_rule_removed",
        entity_type="subscriber_symbol_settings",
        entity_id=rule.id,
        metadata={"symbol": root},
        ip_address=client_ip(request),
    )
    db.commit()
