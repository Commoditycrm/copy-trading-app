"""Webull paper accounts go to Webull's sandbox hosts.

Paper keys only authenticate against the paper environment; live keys only
against the live one. Webull answers a mismatch with 401 "ensure you are
connecting to the correct environment" — which is what connecting paper keys
to the (only) live host produced before this existed.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.brokers.webull as wb


class _FakeApiClient:
    def __init__(self, key, secret, region, **kw):
        self.region, self.endpoints = region, []

    def add_endpoint(self, region_id, endpoint, *a):
        self.endpoints.append((region_id, endpoint))


@pytest.fixture
def built(monkeypatch):
    import webull.core.client as core
    import webull.trade.trade_client as tc

    made = []
    monkeypatch.setattr(core, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(tc, "TradeClient", lambda api: made.append(api) or SimpleNamespace(api=api))
    monkeypatch.setattr(wb, "_suppress_sdk_file_logger", lambda api: None)
    monkeypatch.setattr(wb, "set_per_account_token_dir", lambda api, key: None)
    wb._trade_clients.clear()
    yield made
    wb._trade_clients.clear()


def test_a_paper_client_talks_to_the_sandbox_host(built):
    wb.trade_client_for("paper-key", "s", "us", paper=True)
    assert built[0].endpoints == [("us", "api.sandbox.webull.com")]


def test_a_live_client_keeps_the_sdk_default_host(built):
    wb.trade_client_for("live-key", "s", "us", paper=False)
    assert built[0].endpoints == []


def test_the_adapter_reads_paper_from_its_credentials(built):
    wb.WebullAdapter({"app_key": "k1", "app_secret": "s", "account_id": "A", "paper": True})._trade_client()
    assert built[0].endpoints == [("us", "api.sandbox.webull.com")]
    assert wb.WebullAdapter({"app_key": "k2", "app_secret": "s"}).paper is False


def test_the_listener_streams_paper_events_from_the_sandbox():
    import inspect

    from app.services import webull_listener as wl

    src = inspect.getsource(wl)
    assert "WEBULL_PAPER_EVENTS_HOST if creds.get(\"paper\") else None" in src
    assert "bool(creds.get(\"paper\", False))" in src
    assert wb.WEBULL_PAPER_EVENTS_HOST == "events-api.sandbox.webull.com"


def test_connect_and_account_listing_carry_the_flag():
    import inspect

    from app.api import brokers
    from app.schemas.broker import ListWebullAccountsIn, WebullCredentialsIn

    assert WebullCredentialsIn.model_fields["paper"].default is False
    assert ListWebullAccountsIn.model_fields["paper"].default is False
    src = inspect.getsource(brokers)
    assert 'creds["paper"] = bool(payload.webull.paper)' in src
    assert '"paper": bool(payload.paper),' in src
