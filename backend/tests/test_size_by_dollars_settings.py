"""Sizing by dollars: the settings, and what the channel cards / summary say."""
import uuid
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.discord_sources import _apply_settings, _settings_out
from app.models.settings import TraderSettings
from app.schemas.discord import DiscordSettingsIn


def _ts():
    return TraderSettings(user_id=uuid.uuid4(), discord_trim_count=3, discord_extra_trims=[],
                          discord_quantity_multiplier=4, discord_size_mode="contracts")


def _patch(ts, **body):
    _apply_settings(ts, DiscordSettingsIn(**body), SimpleNamespace(id=uuid.uuid4()))
    return ts


def test_switching_to_dollars_with_an_amount():
    ts = _patch(_ts(), size_dollars="$1,500", size_mode="dollars")
    assert ts.discord_size_mode == "dollars" and ts.discord_size_dollars == D("1500.00")
    out = _settings_out(ts)
    assert out.size_mode == "dollars" and out.size_dollars == "1500"


def test_dollars_without_an_amount_is_refused():
    with pytest.raises(HTTPException, match="dollars per entry first"):
        _patch(_ts(), size_mode="dollars")


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "2000000"])
def test_a_bad_amount_is_refused(bad):
    with pytest.raises(HTTPException):
        _patch(_ts(), size_dollars=bad)


def test_back_to_contracts_keeps_the_amount_for_later():
    ts = _patch(_ts(), size_dollars="500", size_mode="dollars")
    ts = _patch(ts, size_mode="contracts")
    assert ts.discord_size_mode == "contracts" and ts.discord_size_dollars == D("500.00")


# ── one way to size ─────────────────────────────────────────────────────────

from app.services.discord_channel_settings import active_sizing  # noqa: E402


def _s(**kw):
    base = dict(discord_quantity_multiplier=4, discord_size_mode="contracts", discord_size_dollars=None,
                discord_max_per_contract=None, discord_max_per_order=None, discord_size_cap=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_dollars_switch_everything_else_off():
    r = active_sizing(_s(discord_size_mode="dollars", discord_size_dollars=D(500),
                         discord_max_per_contract=D(300), discord_max_per_order=D(1000)))
    assert r == {"mode": "dollars", "contracts": None, "dollars": D(500),
                 "max_per_contract": None, "max_per_order": None, "cap": "none"}


@pytest.mark.parametrize("cap, per_contract, per_order", [
    ("per_contract", D(300), None),
    ("per_order", None, D(1000)),
    ("none", None, None),
])
def test_contracts_take_only_the_chosen_cap(cap, per_contract, per_order):
    r = active_sizing(_s(discord_max_per_contract=D(300), discord_max_per_order=D(1000), discord_size_cap=cap))
    assert (r["contracts"], r["max_per_contract"], r["max_per_order"]) == (4, per_contract, per_order)


@pytest.mark.parametrize("pc, po, cap", [(D(300), D(1000), "per_contract"), (None, D(1000), "per_order"),
                                         (None, None, "none")])
def test_never_chosen_takes_the_cap_that_has_a_value(pc, po, cap):
    assert active_sizing(_s(discord_max_per_contract=pc, discord_max_per_order=po))["cap"] == cap


def test_choosing_a_cap_is_saved():
    ts = _patch(_ts(), size_cap="per_order")
    assert ts.discord_size_cap == "per_order" and _settings_out(ts).size_cap == "per_order"
