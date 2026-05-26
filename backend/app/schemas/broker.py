import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.broker_account import BrokerName


class AlpacaCredentialsIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=200)
    api_secret: str = Field(min_length=8, max_length=200)
    paper: bool = True


class WebullCredentialsIn(BaseModel):
    """All five fields are required. ``mfa_code`` is the SMS/email code the
    user just received after hitting ``/api/brokers/webull/start-mfa``;
    if it's stale Webull rejects the login and the user has to restart
    the flow. ``trade_pin`` is the 6-digit PIN they set in Webull's
    mobile app for trade confirmation — without it we can't place
    orders, so we collect it up front rather than blocking the first
    copy."""

    username: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=4, max_length=200)
    mfa_code: str = Field(min_length=3, max_length=20)
    trade_pin: str = Field(min_length=4, max_length=12)
    paper: bool = True


class StartWebullMfaIn(BaseModel):
    """Step 1 of the Webull connect flow: trigger Webull to send the MFA
    code. We don't store anything yet — the user comes back with
    ``WebullCredentialsIn`` on the second call."""

    username: str = Field(min_length=3, max_length=200)
    paper: bool = True


class StartWebullMfaOut(BaseModel):
    sent: bool
    message: str


class StartSnaptradeIn(BaseModel):
    """Step 1 of the SnapTrade connect flow: returns the hosted portal
    URL. ``broker_slug`` is optional — pass e.g. "ROBINHOOD" to skip
    SnapTrade's broker picker, or leave unset to let the user choose."""

    label: str = Field(min_length=1, max_length=120)
    broker_slug: str | None = None
    paper: bool = False


class StartSnaptradeOut(BaseModel):
    portal_url: str
    # SnapTrade's user secret. We DON'T return this to the browser as
    # plain text on a normal connect — we persist it server-side before
    # generating the portal URL. Field reserved for future flows where
    # we might let the client poll a one-time token.


class FinishSnaptradeIn(BaseModel):
    """Step 2: called after the user returns from the portal. We list
    the user's authorizations on SnapTrade and pick the newest one as
    the connection to attach. ``label`` carries through from start."""

    label: str = Field(min_length=1, max_length=120)


class ConnectBrokerIn(BaseModel):
    broker: BrokerName
    label: str = Field(min_length=1, max_length=120)
    # Exactly one credential block matching `broker` should be populated.
    # SnapTrade has its own two-step flow (start-portal → finish) and
    # doesn't use this generic shape.
    alpaca: AlpacaCredentialsIn | None = None
    webull: WebullCredentialsIn | None = None


class BrokerAccountOut(BaseModel):
    id: uuid.UUID
    broker: BrokerName
    label: str
    is_paper: bool
    supports_fractional: bool
    broker_account_number: str | None
    connection_status: str
    last_error: str | None
    created_at: datetime

    cash: Decimal | None = None
    buying_power: Decimal | None = None
    total_equity: Decimal | None = None
    currency: str | None = None
    balance_updated_at: datetime | None = None

    model_config = {"from_attributes": True}
