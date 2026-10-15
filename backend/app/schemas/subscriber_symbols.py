"""Shapes for a subscriber's per-symbol copy settings."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SymbolRuleIn(BaseModel):
    """What the UI sends for one symbol.

    ``settings`` is the COMPLETE set of overrides for that symbol — sending it
    whole is what lets a field be cleared, which a merge could not express.
    """

    settings: dict[str, Any] = Field(default_factory=dict)
    enabled: bool | None = None


class SymbolRuleOut(BaseModel):
    """One symbol's rule, and what it actually resolves to.

    Both halves are needed by the UI: ``settings`` / ``overridden`` say which
    fields the subscriber chose, ``effective`` says what applies once the
    inherited values are filled in. Showing only the first can't render a form;
    showing only the second can't mark anything as custom.
    """

    symbol: str
    enabled: bool
    # Which fields this symbol sets itself — everything else is inherited.
    overridden: list[str]
    settings: dict[str, Any]
    effective: dict[str, Any]
    # "alerts" (exits follow the trader) or "ladder" (this symbol's own rungs).
    exit_mode: str


class SymbolRulesOut(BaseModel):
    symbols: list[SymbolRuleOut]


class SymbolModeIn(BaseModel):
    """Which mode the subscriber wants: Advanced Settings on or off."""

    enabled: bool


class SymbolModeOut(BaseModel):
    advanced_settings_enabled: bool
