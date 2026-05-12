import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class PortalUrlOut(BaseModel):
    """One-time URL the user opens to link a brokerage at SnapTrade."""

    redirect_uri: str


class BrokerAccountOut(BaseModel):
    id: uuid.UUID
    broker: str
    label: str
    is_paper: bool
    supports_fractional: bool
    snaptrade_account_id: str
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


class SyncResultOut(BaseModel):
    added: int
    removed: int
    accounts: list[BrokerAccountOut]
