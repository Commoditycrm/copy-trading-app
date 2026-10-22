"""The ladder's take-profit pass logs its outcome every tick for every guard.
Only the no-change steady state is demoted to DEBUG; every outcome that means
an order was placed, cancelled, refused, replaced or a position is closing must
stay at INFO, where the QA/prod logs show it."""
import logging

import pytest

from app.services import pnl_poller

STEADY = ["no take-profit (stop: idle)", "no take-profit (stop: in sync)"]

# Real outcome strings from discord_take_profit.reconcile / discord_stop_orders.
VISIBLE = [
    "market closed",
    "backing off",
    "in sync (stop: in sync)",
    "no take-profit (stop: placed 3 @ 1.25)",
    "no take-profit (stop: closed (stop refused))",
    "no take-profit (stop: backing off (recent rejection))",
    "cancel not confirmed — asking again next pass (stop: idle)",
    "replacing — the new take-profit goes out next pass",
    "position being closed (stop: exit sent)",
    "refused — backing off",
    "placed trim 1: 2 @ 1.40",
    "take-profit kept: still working",
    "trim 1 filled; no take-profit (stop: idle)",
]


@pytest.mark.parametrize("outcome", STEADY)
def test_steady_state_is_debug(caplog, outcome):
    with caplog.at_level(logging.DEBUG, logger=pnl_poller.log.name):
        pnl_poller._log_take_profit("SPY", outcome)
    [rec] = caplog.records
    assert rec.levelno == logging.DEBUG
    assert rec.getMessage() == f"take-profit: SPY — {outcome}"


@pytest.mark.parametrize("outcome", VISIBLE)
def test_every_action_outcome_stays_info(caplog, outcome):
    with caplog.at_level(logging.DEBUG, logger=pnl_poller.log.name):
        pnl_poller._log_take_profit("TSLA", outcome)
    [rec] = caplog.records
    assert rec.levelno == logging.INFO
    assert rec.getMessage() == f"take-profit: TSLA — {outcome}"


def test_steady_state_hidden_at_the_default_info_level(caplog):
    with caplog.at_level(logging.INFO, logger=pnl_poller.log.name):
        pnl_poller._log_take_profit("SPY", "no take-profit (stop: idle)")
        pnl_poller._log_take_profit("SPY", "placed trim 1: 2 @ 1.40")
    assert [r.getMessage() for r in caplog.records] == ["take-profit: SPY — placed trim 1: 2 @ 1.40"]
