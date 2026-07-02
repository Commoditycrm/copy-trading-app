import uuid
from datetime import datetime

from pydantic import BaseModel


class FollowRequestCreate(BaseModel):
    """Subscriber asks to follow a trader."""
    trader_id: uuid.UUID


class FollowRequestOut(BaseModel):
    id: uuid.UUID
    subscriber_id: uuid.UUID
    trader_id: uuid.UUID
    status: str
    decided_at: datetime | None = None
    created_at: datetime

    # Display fields — populated per view. The trader's "incoming" list needs
    # the subscriber's identity; the subscriber's own list needs the trader's.
    subscriber_name: str | None = None
    subscriber_email: str | None = None
    trader_name: str | None = None
    trader_business_name: str | None = None

    model_config = {"from_attributes": True}
