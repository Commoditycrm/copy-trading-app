"""Which settings apply to one symbol, for one subscriber.

A copy subscriber has one set of rules for everything they mirror. A symbol rule
says "this one is different" — its own size, and its own way out — and inherits
everything it does not mention.

The resolution order, and the whole point of the module:

    1. the symbol's own override, when it set that field;
    2. otherwise the subscriber's global settings;
    3. otherwise the default that field had before it existed.

``resolve()`` returns an object read exactly like a settings row, so callers do
not branch on whether a symbol is customised — they ask for a value and get the
right one. That is what lets the exit-ladder engine (services/discord_ladder and
everything downstream of it) work for a subscriber unchanged: it reads
``getattr(ts, "discord_…")`` and never learns where the value came from.

Same mechanism as a Discord channel's own settings
(services/discord_channel_settings), keyed on a SYMBOL instead of a channel.

Nothing here applies until a row exists. With no row ``resolve()`` hands back
the subscriber's own settings object, so every copy path behaves exactly as it
does today — which is what keeps ordinary trader copying untouched.
"""
from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.settings import SubscriberSettings, TraderSettings
from app.models.subscriber_symbol_settings import SubscriberSymbolSettings

# ── what a symbol may override ──────────────────────────────────────────────
# Sizing, in the COPY model: a mirror is the trader's quantity scaled by a
# multiplier, then capped. Deliberately NOT the Discord model (a fixed contract
# count, or dollars per entry) — that ignores what the trader did, and it
# contradicts the instrument-lock rule, which fixes a position's multiplier at
# first entry so later adds and exits stay proportional.
SIZING_FIELDS = (
    # The DISCORD sizing fields, deliberately — an advanced symbol is sized the
    # way a Discord channel is: a fixed number of contracts per entry, or a
    # dollar amount, with at most one cap. Not the copy multiplier, so an entry
    # no longer scales with the trader's quantity while Advanced is on.
    #
    # Same names the Discord settings use so discord_channel_settings.
    # active_sizing() reads a symbol's values with no translation, and the one
    # sizing contract stays in one place.
    "discord_quantity_multiplier",   # contracts per entry, in "contracts" mode
    "discord_size_mode",             # "contracts" | "dollars"
    "discord_size_dollars",
    "discord_size_cap",              # "none" | "per_contract" | "per_order"
    "discord_max_per_contract",
    "discord_max_per_order",
)

# The exit ladder, named as the ladder engine reads them so no translation is
# needed anywhere downstream.
LADDER_FIELDS = (
    "discord_auto_trim",            # the exit mode: see exit_mode() below
    "discord_manual_exit",
    "discord_tp_orders",
    "discord_trim_profit_gate_pct",
    "discord_trim_stop_pct",
    "discord_trim_qty_pct",
    "discord_trim2_profit_gate_pct",
    "discord_trim2_stop_pct",
    "discord_trim2_qty_pct",
    "discord_trim3_profit_gate_pct",
    "discord_trim3_stop_pct",
    "discord_trim3_qty_pct",
    "discord_trim_count",
    "discord_extra_trims",
    "discord_fill_stop_pct",
    "discord_stop_trails",
    "discord_trim_price_threshold",
    "discord_trim_trail_amount",
)

# The reserved scope for the common layer. A real ticker is never "ALL", and
# the (user, symbol) uniqueness constraint already makes it one per subscriber —
# so the common settings are just another row, read through the same overlay.
ALL_SYMBOLS = "ALL"

SYMBOL_FIELDS = SIZING_FIELDS + LADDER_FIELDS
_FIELDS = frozenset(SYMBOL_FIELDS)

# What a ladder field means when NOTHING has set it — neither the symbol nor the
# subscriber, who has no ladder columns at all. These are the values that make a
# fresh symbol rule behave like the ladder's own defaults rather than like a
# half-configured one.
_LADDER_DEFAULTS: dict[str, Any] = {
    # Sizing, for a subscriber whose row has no discord_* columns: one contract
    # per entry and no cap — what active_sizing() treats as the default.
    "discord_quantity_multiplier": 1,
    "discord_size_mode": "contracts",
    "discord_size_dollars": None,
    "discord_size_cap": "none",
    "discord_max_per_contract": None,
    "discord_max_per_order": None,
    "discord_auto_trim": False,       # "On Alerts": exits follow the trader
    "discord_manual_exit": False,
    "discord_tp_orders": False,
    "discord_trim_count": 3,
    "discord_extra_trims": [],
    "discord_fill_stop_pct": None,
    "discord_stop_trails": None,
    "discord_trim_price_threshold": None,
    "discord_trim_trail_amount": None,
}


def _column(name: str):
    """The column a field belongs to — subscriber sizing first, then the ladder.

    Two namespaces meet here: sizing lives on SubscriberSettings, the ladder on
    TraderSettings. Looking both up keeps one stored value readable as the right
    Python type whichever side it came from.
    """
    for model in (SubscriberSettings, TraderSettings):
        col = model.__table__.columns.get(name)
        if col is not None:
            return col
    return None


def _from_json(name: str, raw: Any) -> Any:
    """A stored override back in the column's own Python type."""
    if raw is None:
        return None
    col = _column(name)
    pytype = getattr(getattr(col, "type", None), "python_type", None)
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


class SymbolSettings:
    """Read like a settings row: this symbol's overrides, then the subscriber's.

    Anything outside SYMBOL_FIELDS — the kill switch, daily limits, the trader
    being followed — is account-wide and comes from the subscriber's row
    untouched. A symbol rule can change how much and how you exit, never who you
    follow or whether copying is on.
    """

    def __init__(self, base: Any, values: dict[str, Any] | None):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_values", dict(values or {}))

    def __getattr__(self, name: str) -> Any:
        values = object.__getattribute__(self, "_values")
        if name in _FIELDS and name in values:
            return _from_json(name, values[name])
        base = object.__getattribute__(self, "_base")
        # A ladder field the subscriber has no column for: the ladder's own
        # default, not None — None would read as "no ladder configured at all".
        if name in _LADDER_DEFAULTS and not hasattr(base, name):
            default = _LADDER_DEFAULTS[name]
            return list(default) if isinstance(default, list) else default
        return getattr(base, name, None)

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in _FIELDS:
            raise AttributeError(f"{name} is account-wide and can't be set per symbol")
        object.__getattribute__(self, "_values")[name] = _to_json(value)

    def to_json(self) -> dict[str, Any]:
        return dict(object.__getattribute__(self, "_values"))

    @property
    def overridden(self) -> list[str]:
        """Which fields this symbol sets itself — what the UI marks as custom."""
        return [f for f in SYMBOL_FIELDS if f in object.__getattribute__(self, "_values")]


def normalize(symbol: str | None) -> str:
    """The underlying root, as rules are keyed: "aapl " -> "AAPL".

    Never an OCC contract. A rule for AAPL covers AAPL shares and every AAPL
    option, because the exit ladder already keys on the root.
    """
    return (symbol or "").strip().upper()


def rule_for(db: Session, user_id: uuid.UUID, symbol: str | None) -> SubscriberSymbolSettings | None:
    """This subscriber's ENABLED rule for the symbol, if any."""
    root = normalize(symbol)
    if not root:
        return None
    return db.execute(
        select(SubscriberSymbolSettings).where(
            SubscriberSymbolSettings.user_id == user_id,
            SubscriberSymbolSettings.symbol == root,
            SubscriberSymbolSettings.enabled.is_(True),
        ).limit(1)
    ).scalars().first()


def advanced_enabled(base: Any) -> bool:
    """Is this subscriber on Advanced Settings rather than Risk Controls?"""
    return bool(getattr(base, "advanced_settings_enabled", False))


def resolve(db: Session, user_id: uuid.UUID, symbol: str | None) -> Any:
    """The settings that apply to this subscriber for this symbol.

    Three layers, most specific first:

      1. the SYMBOL's own rule — what it explicitly sets;
      2. the ALL-Symbols rule — the common configuration, for anything the
         symbol did not set;
      3. the subscriber's saved settings — for anything neither set, so
         switching to Advanced never silently changes a value they had.

    Returns the subscriber's own row untouched when Advanced Settings is off,
    or when nothing is configured — the same object every caller used before
    symbol rules existed. That identity is what makes Risk Controls mode
    behave exactly as it does today: there is no overlay in the path at all.
    """
    base = db.get(SubscriberSettings, user_id)
    if not advanced_enabled(base):
        return base
    common = rule_for(db, user_id, ALL_SYMBOLS)
    own = rule_for(db, user_id, symbol) if normalize(symbol) != ALL_SYMBOLS else None
    if common is None and own is None:
        return base
    # The symbol's own values land ON TOP of the common ones, key by key — so a
    # symbol overrides only what it names and inherits the rest. Updating the
    # common layer therefore cannot erase an individual override, and clearing
    # one hands that field back to the common layer rather than to the defaults.
    merged: dict[str, Any] = {}
    if common is not None:
        merged.update(common.settings or {})
    if own is not None:
        merged.update(own.settings or {})
    return SymbolSettings(base, merged)


def has_rule(db: Session, user_id: uuid.UUID, symbol: str | None) -> bool:
    """Is this symbol customised? For callers that only need to know whether to
    take the symbol path at all."""
    return rule_for(db, user_id, symbol) is not None


def sizing_for(db: Session, user_id: uuid.UUID, symbol: str | None) -> dict | None:
    """How to size an entry in this symbol, or None to size it as before.

    None whenever Advanced is off or nothing is configured — the caller then
    scales the trader's quantity by the subscriber's multiplier, exactly as it
    always has.

    Otherwise the Discord sizing contract, read by the SAME function the Discord
    path uses (active_sizing): a fixed number of contracts per entry, or a
    dollar amount, with at most one cap. An advanced entry no longer tracks what
    the trader traded — that is what choosing a fixed size means.
    """
    base = db.get(SubscriberSettings, user_id)
    if not advanced_enabled(base):
        return None
    ts = resolve(db, user_id, symbol)
    if ts is base:
        return None
    from app.services.discord_channel_settings import active_sizing  # noqa: PLC0415

    return active_sizing(ts)


def settings_for_guard(db: Session, user_id: uuid.UUID, guard: Any) -> Any | None:
    """The symbol settings a POSITION's ladder should run on, or None.

    None whenever this is not a symbol-rule position — no guard, no symbol, or
    no rule for it — so the caller falls back to whatever it used before. A
    subscriber mirroring a trader has no Discord channel behind their position,
    which is exactly the gap this fills: the ladder engine needs a settings
    object and there was none to give it.

    Only when the symbol is on its OWN ladder. "On Alerts" means exits follow
    the trader, so there is no ladder to run and returning settings here would
    start one.
    """
    symbol = getattr(guard, "symbol", None) if guard is not None else None
    if not symbol:
        return None
    # The rule lookup FIRST, and bail on a miss. This runs per guard per tick
    # for every position without a channel behind it, so the common answer —
    # "no rule" — must cost one indexed query and nothing else.
    rule = rule_for(db, user_id, symbol)
    if rule is None:
        return None
    ts = SymbolSettings(db.get(SubscriberSettings, user_id), rule.settings or {})
    return ts if exit_mode(ts) == "ladder" else None


def trims_follow_trader(db: Session, user_id: uuid.UUID, symbol: str | None) -> bool:
    """Should a trader's TRIM be mirrored for this symbol?

    False once the symbol is on its own ladder: its rungs decide when to take
    profit, and mirroring the trader's trim as well would sell the same
    position twice over. A trader's FULL close is still mirrored — that is the
    line the ladder must not cross, or a subscriber could be left holding a
    position the trader has exited.
    """
    ts = resolve(db, user_id, symbol)
    base = db.get(SubscriberSettings, user_id)
    if ts is base:
        return True
    return exit_mode(ts) != "ladder"


# The fields that carry a trim's profit target, across the named rungs.
_GATE_FIELDS = (
    "discord_trim_profit_gate_pct",
    "discord_trim2_profit_gate_pct",
    "discord_trim3_profit_gate_pct",
)


def has_ladder(ts: Any) -> bool:
    """Does this scope have a target of its OWN that can fire?

    Its own, not the resolved value: the ladder's built-in Trim 1 default is
    20%, so reading the resolved rungs would say "yes" for a rule that set
    nothing but a size. Only what the subscriber (or the ALL layer) actually
    configured counts.

    A target of zero does not count either — zero means "keep waiting" to the
    engine, because "reached 0% profit" is true the moment a position is up a
    cent.

    This is what stops a sizing-only rule taking over the exits: it would stop
    the trader's trims being mirrored while firing nothing itself, and nobody
    would trim the position.
    """
    values = getattr(ts, "to_json", None)
    values = values() if callable(values) else {}
    for field in _GATE_FIELDS:
        raw = values.get(field)
        if raw in (None, ""):
            continue
        try:
            if Decimal(str(raw)) > 0:
                return True
        except (InvalidOperation, ValueError, TypeError):
            continue
    # Trims 4+ live in a list of their own.
    for extra in (values.get("discord_extra_trims") or []):
        try:
            if Decimal(str((extra or {}).get("profit_gate_pct") or 0)) > 0:
                return True
        except (InvalidOperation, ValueError, TypeError):
            continue
    return False


def exit_mode(ts: Any) -> str:
    """How this symbol gets out — the choice under the ladder.

      * ``"alerts"``  the trader's exits are mirrored, as they always have been;
      * ``"ladder"``  the symbol's own rungs fire on their own (auto-trim), and
        the trader's TRIMS are not mirrored for it. A trader's full close is
        still honoured, so the subscriber can never be left holding a position
        the trader has exited.

    Stored as ``discord_auto_trim`` rather than a new column: the ladder engine
    already reads that field to decide whether rungs fire by themselves, so the
    two options are a setting the engine understands, not a parallel concept.
    """
    return ("ladder"
            if bool(getattr(ts, "discord_auto_trim", False)) and has_ladder(ts)
            else "alerts")


__all__ = [
    "LADDER_FIELDS",
    "SIZING_FIELDS",
    "SYMBOL_FIELDS",
    "SymbolSettings",
    "exit_mode",
    "has_rule",
    "normalize",
    "resolve",
    "rule_for",
]
