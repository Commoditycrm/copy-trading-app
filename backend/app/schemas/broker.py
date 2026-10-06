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
    """Direct Webull (official OpenAPI) credentials the trader generates in
    Webull's developer portal (Trading API → Retail Individual → Obtain API
    Key). ``account_id`` is the Webull account_id (NOT the account number) —
    the value returned by ``account_v2.get_account_list()``. region is US.

    Stored Fernet-encrypted, exactly like Alpaca keys. Powers the real-time
    gRPC trade signal; gated behind settings.webull_direct_enabled."""

    app_key: str = Field(min_length=8, max_length=200)
    app_secret: str = Field(min_length=8, max_length=200)
    account_id: str = Field(min_length=4, max_length=120)
    region_id: str = Field(default="us", max_length=8)
    # Paper keys come from Webull's test environment and only authenticate
    # against its sandbox host; live keys only against the live one.
    paper: bool = False


class ListWebullAccountsIn(BaseModel):
    """Step 1 of the direct-Webull connect: exchange the API keys for the list of
    accounts they can trade, so the user PICKS one instead of typing an opaque
    ``account_id`` they have no reliable way to look up."""

    app_key: str = Field(min_length=8, max_length=200)
    app_secret: str = Field(min_length=8, max_length=200)
    region_id: str = Field(default="us", max_length=8)
    paper: bool = False


class WebullAccountOut(BaseModel):
    """One selectable Webull account. ``account_id`` is what gets stored in the
    credentials; everything else exists so the user can tell their accounts
    apart — the balance especially, since the failure this prevents is linking a
    real-but-empty account (a Futures or unfunded Cash one) and having every
    mirror order trade there."""

    account_id: str
    account_number: str | None = None
    account_type: str | None = None
    currency: str | None = None
    total_equity: Decimal | None = None
    buying_power: Decimal | None = None


class IbkrCredentialsIn(BaseModel):
    """Self-service OAuth 1.0a material from IBKR Client Portal
    (Settings → API → OAuth). ``access_token_secret`` is the base64,
    RSA-encrypted secret IBKR shows once; the two private keys are the PEMs
    the user generated for that consumer; ``dh_prime`` is the hex modulus
    (or the whole ``dhparam.pem``). ``account_id`` is the IBKR account
    number (``U1234567`` live, ``DU…`` paper). The adapter validates that
    the keys parse and that the secret decrypts before anything is saved."""

    consumer_key: str = Field(min_length=4, max_length=200)
    access_token: str = Field(min_length=4, max_length=500)
    access_token_secret: str = Field(min_length=20, max_length=8000)
    private_signature_key: str = Field(min_length=100, max_length=16000)
    private_encryption_key: str = Field(min_length=100, max_length=16000)
    dh_prime: str = Field(min_length=100, max_length=16000)
    account_id: str = Field(min_length=2, max_length=40)
    paper: bool = False
    # OAuth realm. Self-service consumers use "limited_poa"; leave unset.
    realm: str | None = Field(default=None, max_length=40)


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
    alpaca: AlpacaCredentialsIn  | None = None
    ibkr:   IbkrCredentialsIn    | None = None
    webull: WebullCredentialsIn  | None = None


class BrokerAccountOut(BaseModel):
    id: uuid.UUID
    broker: BrokerName
    label: str
    is_paper: bool
    supports_fractional: bool
    broker_account_number: str | None
    # Underlying broker for aggregator-routed accounts (broker=snaptrade).
    # NULL for direct-API brokers because `broker` itself is already the
    # real name.
    brokerage_name: str | None = None
    connection_status: str
    last_error: str | None
    created_at: datetime

    cash: Decimal | None = None
    buying_power: Decimal | None = None
    total_equity: Decimal | None = None
    currency: str | None = None
    balance_updated_at: datetime | None = None

    # Listener-gating flags surfaced in the Brokers UI (Auto Pull Orders +
    # children). The Brokers page renders these as the three checkboxes;
    # PATCH /api/brokers/{id}/settings flips them.
    auto_pull_orders: bool = True
    bring_open_orders: bool = True
    bring_filled_orders: bool = True

    # One-off message for the toast after connect / activate — e.g. that another
    # account on the same Webull app key was deactivated. Transient, never stored.
    notice: str | None = None

    # Effective steady refresh interval (seconds) for this account's Day P&L
    # surfaces (calendar today + top card). From broker capabilities (Alpaca
    # reuses the runtime knob); the frontend reads it here instead of hardcoding
    # per-broker intervals. 30 is the safe default.
    day_pnl_refresh_interval_s: int = 30

    model_config = {"from_attributes": True}


class BrokerAccountSettingsIn(BaseModel):
    """Partial-update payload for the three listener-gating flags. Any
    field left unset is unchanged on the server."""

    auto_pull_orders: bool | None = None
    bring_open_orders: bool | None = None
    bring_filled_orders: bool | None = None
