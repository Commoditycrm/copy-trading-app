import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.broker_account import BrokerName


class AlpacaCredentialsIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=200)
    api_secret: str = Field(min_length=8, max_length=200)
    paper: bool = True


class ConnectBrokerIn(BaseModel):
    broker: BrokerName
    label: str = Field(min_length=1, max_length=120)
    # Exactly one credential block matching `broker` should be populated.
    alpaca: AlpacaCredentialsIn | None = None


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
