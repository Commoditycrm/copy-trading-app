"""Per-channel setting: what a risky alert does on the week's last trading day.

"normal" — half size, as on any day; "half" — half of that (a quarter); "full" —
the risky words are ignored and the entry is full size. The last trading day is
Friday, or Thursday when Friday is a market holiday.
"""
import os
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal as D
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException

import app.api.discord_sources as ds
from app.models.settings import TraderSettings
from app.schemas.discord import DiscordSettingsIn
from app.services import discord_execution as ex
from app.services import market_hours
from app.services.market_hours import is_last_trading_day_of_week as last_day


# ── which day is the last trading day ────────────────────────────────────────

def test_an_ordinary_friday_is_the_last_trading_day():
    assert last_day(date(2026, 10, 16)) is True               # Friday


def test_an_ordinary_thursday_is_not():
    assert last_day(date(2026, 10, 15)) is False


def test_thursday_is_the_last_trading_day_before_good_friday():
    assert last_day(date(2026, 4, 2)) is True                 # Good Friday is 2026-04-03
    assert last_day(date(2026, 4, 3)) is False                # the holiday itself


def test_weekends_are_not():
    assert last_day(date(2026, 10, 17)) is False
    assert last_day(date(2026, 10, 18)) is False


# ── sizing a risky entry ────────────────────────────────────────────────────

def _qty(multiplier, half, factor=D("0.5"), note=None):
    sizing = ex.Sizing(multiplier=multiplier, risky_factor=factor, risky_note=note)
    res = {}
    return ex._entry_quantity({"half_size": half}, sizing, res), res


def test_a_risky_entry_is_half_size_normally():
    assert _qty(5, True)[0] == D(3)                            # 2.5 rounds up


def test_half_of_the_usual_risky_size_is_a_quarter():
    qty, res = _qty(5, True, D("0.25"), "a quarter — last trading day")
    assert qty == D(2)                                         # 1.25 rounds up
    assert "last trading day" in res["quantity"]


def test_a_quarter_never_rounds_to_nothing():
    assert _qty(1, True, D("0.25"))[0] == D(1)


def test_dollar_sizing_takes_a_quarter_of_the_amount():
    sizing = ex.Sizing(mode="dollars", dollars=D("400"), risky_factor=D("0.25"),
                       risky_note="a quarter — last trading day")
    res = {}
    qty = ex._dollar_quantity(sizing, D("0.50"), True, D("0.25"), res)
    assert qty == D(2)                                         # $100 ÷ $50 a contract
    assert "$100.00 per entry" in res["quantity"]


# ── the rule applied to an alert ────────────────────────────────────────────

def _run(monkeypatch, mode, *, friday=True, half=True, ts=None):
    ts = ts if ts is not None else SimpleNamespace(discord_last_day_risky=mode)
    monkeypatch.setattr(market_hours, "now_et", lambda: datetime(2026, 10, 16 if friday else 15, 10, 0))
    sig = {"action": "buy", "half_size": half}
    msg = SimpleNamespace(parsed_signal=sig)
    sizing = ex.Sizing(multiplier=4)
    out = ds._last_day_risky(ts, msg, sig, sizing)
    return out, sizing, msg


def test_do_nothing_leaves_it_half(monkeypatch):
    out, sizing, _ = _run(monkeypatch, "normal")
    assert out["half_size"] is True and sizing.risky_factor == D("0.5")


def test_half_on_the_last_day_makes_it_a_quarter(monkeypatch):
    out, sizing, _ = _run(monkeypatch, "half")
    assert out["half_size"] is True and sizing.risky_factor == D("0.25")


def test_full_on_the_last_day_ignores_the_risky_words(monkeypatch):
    out, sizing, msg = _run(monkeypatch, "full")
    assert out["half_size"] is False and out["last_day_full"] is True
    # Recorded on the alert, so a later average treats the position as full.
    assert msg.parsed_signal["half_size"] is False
    assert "risky words ignored" in sizing.risky_note


def test_any_other_day_is_untouched(monkeypatch):
    out, sizing, _ = _run(monkeypatch, "full", friday=False)
    assert out["half_size"] is True and sizing.risky_note is None


def test_an_ordinary_alert_is_untouched(monkeypatch):
    out, sizing, _ = _run(monkeypatch, "half", half=False)
    assert out["half_size"] is False and sizing.risky_factor == D("0.5")


def test_dont_take_risky_trades_skips_a_risky_entry_on_the_last_day(monkeypatch):
    with pytest.raises(ds.LastDayRiskySkip, match="Don't Take Risky Trades"):
        _run(monkeypatch, "skip")


def test_dont_take_risky_trades_leaves_other_days_alone(monkeypatch):
    out, sizing, _ = _run(monkeypatch, "skip", friday=False)
    assert out["half_size"] is True and sizing.risky_factor == D("0.5")


def test_dont_take_risky_trades_leaves_an_ordinary_alert_alone(monkeypatch):
    out, _sizing, _ = _run(monkeypatch, "skip", half=False)
    assert out["half_size"] is False


@pytest.mark.parametrize("extra", [{"double_up": True}, {"add_to_latest": True}])
def test_dont_take_risky_trades_never_blocks_an_average_or_an_add(monkeypatch, extra):
    """An average sizes from the position, not the words — it still goes."""
    monkeypatch.setattr(market_hours, "now_et", lambda: datetime(2026, 10, 16, 10, 0))
    sig = {"action": "BUY", "half_size": True, **extra}
    out = ds._last_day_risky(SimpleNamespace(discord_last_day_risky="skip"),
                             SimpleNamespace(parsed_signal=sig), sig, ex.Sizing(multiplier=4))
    assert out is sig


def test_dont_take_risky_trades_is_saved():
    ts = TraderSettings(user_id=uuid.uuid4())
    ds._apply_settings(ts, DiscordSettingsIn(last_day_risky="skip"), SimpleNamespace(id=uuid.uuid4()))
    assert ts.discord_last_day_risky == "skip" and ds._settings_out(ts).last_day_risky == "skip"


# ── the setting ─────────────────────────────────────────────────────────────

def test_the_account_saves_it():
    ts = TraderSettings(user_id=uuid.uuid4())
    ds._apply_settings(ts, DiscordSettingsIn(last_day_risky="full"), SimpleNamespace(id=uuid.uuid4()))
    assert ts.discord_last_day_risky == "full"
    assert ds._settings_out(ts).last_day_risky == "full"


def test_a_channel_with_its_own_settings_sets_its_own():
    from app.services.discord_channel_settings import ChannelSettings
    account = TraderSettings(user_id=uuid.uuid4(), discord_last_day_risky="normal")
    own = ChannelSettings(account, {})
    ds._apply_settings(own, DiscordSettingsIn(last_day_risky="half"), SimpleNamespace(id=uuid.uuid4()))
    assert own.discord_last_day_risky == "half"
    assert own.to_json()["discord_last_day_risky"] == "half"
    assert account.discord_last_day_risky == "normal", "the account is untouched"


def test_the_channel_s_own_choice_is_what_applies(monkeypatch):
    from app.services.discord_channel_settings import ChannelSettings
    account = TraderSettings(user_id=uuid.uuid4(), discord_last_day_risky="full")
    own = ChannelSettings(account, {"discord_last_day_risky": "half"})
    out, sizing, _ = _run(monkeypatch, None, ts=own)
    assert out["half_size"] is True and sizing.risky_factor == D("0.25")


def test_a_channel_whose_own_copy_predates_it_keeps_the_account_s_choice(monkeypatch):
    """It was account-wide first: an older own copy has no value, and reads the
    account's rather than a default — what applied to it until now."""
    from app.services.discord_channel_settings import ChannelSettings
    account = TraderSettings(user_id=uuid.uuid4(), discord_last_day_risky="full")
    own = ChannelSettings(account, {"discord_quantity_multiplier": 3})
    out, _, _ = _run(monkeypatch, None, ts=own)
    assert out["half_size"] is False


def test_turning_a_channel_s_own_settings_on_copies_the_account_s_choice():
    from app.services.discord_channel_settings import snapshot
    account = TraderSettings(user_id=uuid.uuid4(), discord_last_day_risky="half")
    assert snapshot(account)["discord_last_day_risky"] == "half"


def test_an_unknown_value_is_refused():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        DiscordSettingsIn(last_day_risky="double")
