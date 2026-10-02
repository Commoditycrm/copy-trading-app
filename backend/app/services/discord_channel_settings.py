"""Per-channel alert handling.

A channel either follows the account's Discord settings (``use_account_settings``,
the default) or carries its own copy in ``channel_settings`` — taken from the
account's values when the switch was turned off, then edited. Everything the
Alert handling section holds can be set per channel except AI trimming, which
stays account-wide.

Which channel's settings apply:

* an ENTRY uses the channel the alert came from;
* an EXIT uses the channel that OPENED the position (its exit ladder, trailing
  exit and test/live) — so an auto-trim, which fires through the Self channel,
  still trims a Clint position on Clint's ladder. Unknown origin (a position
  opened by hand) falls back to the alert's channel.

``effective()`` returns an object read exactly like a TraderSettings row, so the
code that consumed the account row reads a channel's values unchanged.
"""
from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.discord_alert_source import DiscordAlertSource
from app.models.settings import TraderSettings

# The TraderSettings columns a channel can override — Alert handling minus AI
# trimming, and minus the trader's fill-broadcast webhook.
CHANNEL_FIELDS = (
    "discord_execution_mode",
    "discord_live_trading",
    "discord_quantity_multiplier",
    "discord_max_per_contract",
    "discord_max_per_order",
    "discord_trail_percent",
    "discord_trim_profit_gate_pct",
    "discord_trim_stop_pct",
    "discord_trim2_profit_gate_pct",
    "discord_trim2_stop_pct",
    "discord_trim3_profit_gate_pct",
    "discord_trim3_stop_pct",
    "discord_auto_trim",
    "discord_manual_exit",
    "discord_trim_qty_pct",
    "discord_trim2_qty_pct",
    "discord_trim3_qty_pct",
    "discord_trim_count",
    "discord_extra_trims",
    "discord_fill_stop_pct",
    "discord_trim_price_threshold",
    "discord_trim_trail_amount",
    "discord_reprice_after_seconds",
    "discord_reprice_pct",
)
_FIELDS = frozenset(CHANNEL_FIELDS)


def _from_json(name: str, raw: Any) -> Any:
    """A stored override back in the column's own Python type."""
    if raw is None:
        return None
    col = TraderSettings.__table__.columns[name]
    pytype = getattr(col.type, "python_type", None)
    try:
        if pytype is Decimal:
            return Decimal(str(raw))
        if pytype is bool:
            return bool(raw)
        if pytype is int:
            return int(raw)
    except (InvalidOperation, ValueError, TypeError, NotImplementedError):
        return None
    return raw


def _to_json(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value


class ChannelSettings:
    """Read like a TraderSettings row: a channel field comes from the channel's
    own values, anything else (AI trimming, copy pause …) from the account."""

    def __init__(self, account: TraderSettings | None, values: dict[str, Any]):
        object.__setattr__(self, "_account", account)
        object.__setattr__(self, "_values", dict(values or {}))

    def __getattr__(self, name: str) -> Any:
        values = object.__getattribute__(self, "_values")
        if name in _FIELDS and name in values:
            return _from_json(name, values[name])
        return getattr(object.__getattribute__(self, "_account"), name, None)

    def __setattr__(self, name: str, value: Any) -> None:
        """Writes land in the channel's values — so the account settings PATCH
        logic can validate and apply a channel's edits unchanged."""
        if name not in _FIELDS:
            raise AttributeError(f"{name} is account-wide and can't be set per channel")
        object.__getattribute__(self, "_values")[name] = _to_json(value)

    def to_json(self) -> dict[str, Any]:
        return dict(object.__getattribute__(self, "_values"))


def snapshot(account: TraderSettings | None) -> dict[str, Any]:
    """The account's current values for every channel field — a channel's
    starting point when it stops following the account."""
    return {f: _to_json(getattr(account, f, None)) for f in CHANNEL_FIELDS
            if account is not None and getattr(account, f, None) is not None}


def effective(db: Session, user_id: uuid.UUID, source_id: uuid.UUID | None):
    """The settings that apply to an alert from ``source_id``: the account row,
    or the channel's own when it doesn't follow the account."""
    account = db.get(TraderSettings, user_id)
    if source_id is None:
        return account
    src = db.get(DiscordAlertSource, source_id)
    if src is None or src.user_id != user_id or src.use_account_settings:
        return account
    return ChannelSettings(account, src.channel_settings or {})


def source_for_order(db: Session, order_id: uuid.UUID | None) -> uuid.UUID | None:
    """The channel whose alert placed ``order_id``, if any."""
    if order_id is None:
        return None
    from app.models.discord_message import DiscordMessage  # noqa: PLC0415

    return db.execute(
        select(DiscordMessage.source_id).where(DiscordMessage.order_id == order_id).limit(1)
    ).scalar_one_or_none()


def for_guard(db: Session, user_id: uuid.UUID, guard) -> Any | None:
    """The settings of the channel that OPENED this position (its exit ladder),
    or None when the opening order isn't a channel's — the caller then uses the
    alert's own channel."""
    # A channel the trader assigned by hand (Positions → Channel) wins over the
    # one whose alert opened the position.
    source_id = getattr(guard, "source_id", None) or source_for_order(
        db, getattr(guard, "entry_order_id", None))
    if source_id is None:
        return None
    return effective(db, user_id, source_id)


def exits_manual(ts: Any) -> bool:
    """Are exits left to the trader under these settings (account row or a
    channel's own)? Then nothing sells on its own: not an exit alert, not
    auto-trim, not AI trimming."""
    return bool(ts is not None and getattr(ts, "discord_manual_exit", False))


def exit_mode(ts: Any) -> str:
    """"manual", "auto" (auto-trim) or "alerts" (wait for the channel's exit
    alerts). Manual wins over auto-trim."""
    if exits_manual(ts):
        return "manual"
    return "auto" if getattr(ts, "discord_auto_trim", False) else "alerts"


def entry_order_type(db: Session, source_id: uuid.UUID | None) -> str:
    """"limit" (the default) or "market" for entries from this channel."""
    if source_id is None:
        return "limit"
    src = db.get(DiscordAlertSource, source_id)
    value = (getattr(src, "entry_order_type", None) or "limit").lower()
    return value if value in ("limit", "market") else "limit"


__all__ = [
    "CHANNEL_FIELDS", "ChannelSettings", "snapshot", "effective",
    "source_for_order", "for_guard", "entry_order_type", "exits_manual", "exit_mode",
]
