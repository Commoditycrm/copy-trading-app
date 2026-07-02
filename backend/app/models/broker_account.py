import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class BrokerName(str, enum.Enum):
    """Brokers we directly integrate with. Adding a new one means writing an
    adapter under app/brokers/."""
    ALPACA = "alpaca"
    # Webull (via the unofficial `webull` Python SDK). Real-time order
    # updates are *polled* (not socket) every ~2s — the SDK does not expose
    # a stable MQTT order channel. See app/services/webull_listener.py.
    WEBULL = "webull"
    # SnapTrade aggregator. User connects through SnapTrade's hosted portal
    # — we never see the underlying broker credentials. Order updates are
    # polled every ~5s. Latency is higher than direct integrations because
    # SnapTrade itself polls the upstream broker, so faster polling on our
    # side buys nothing. See app/brokers/snaptrade.py.
    SNAPTRADE = "snaptrade"
    # Interactive Brokers — direct integration via IBKR's Web API (OAuth 1.0a,
    # per-user self-service consumer credentials). Order updates are polled
    # every ~2–5s (faster than SnapTrade because we talk to IBKR directly).
    # See app/brokers/ibkr.py and app/services/ibkr_listener.py.
    IBKR = "ibkr"
    # Test-only mock broker — see app/brokers/fake.py. NEVER ROUTE A REAL
    # SUBSCRIBER TO THIS. Calls to place_order() sleep + return synthetic
    # results; no order is sent anywhere. Used by
    # scripts/seed_fake_subscribers.py for load-testing the fanout pipeline
    # without hitting Alpaca's rate limits or burning paper accounts.
    FAKE = "fake"


class BrokerAccount(Base, TimestampMixin):
    """One connected brokerage account, owned by one app user.

    Credentials are stored encrypted (Fernet) in `encrypted_credentials` as a
    JSON blob whose shape depends on the broker. For Alpaca it's
    `{"api_key": "...", "api_secret": "...", "paper": true}`.
    """

    __tablename__ = "broker_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # values_callable tells SQLAlchemy to send enum.value (e.g. "alpaca") instead
    # of enum.name ("ALPACA") to Postgres. The DB-side enum was created with the
    # lowercase value, so this keeps Python ↔ Postgres in sync.
    broker: Mapped[BrokerName] = mapped_column(
        Enum(BrokerName, name="broker_name",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_fractional: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Encrypted JSON blob. Decrypt via services.crypto.decrypt_json.
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)

    # Broker's own account number/id for display
    broker_account_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    connection_status: Mapped[str] = mapped_column(String(40), default="connected", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Soft-disconnect marker for the reconnect-history feature. NULL = this
    # is the user's ACTIVE broker; non-NULL = it was disconnected at this
    # time and now lives in "Recent connections" as a reconnectable entry
    # (its encrypted_credentials are retained so reconnect needs no re-entry).
    # Only direct-credential brokers (Alpaca / IBKR) go to history — SnapTrade
    # is still hard-deleted on disconnect because its upstream authorization
    # is revoked and can't be reused. Retention is capped per user in the API.
    disconnected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Underlying broker when this account is routed through an aggregator.
    # For ``broker=snaptrade``, this is the real broker the subscriber
    # connected (e.g. "Webull", "Robinhood", "IBKR") — needed by the trader's
    # fanout view so they can see which actual broker each mirror went to,
    # not the generic "snaptrade" label. For direct-API brokers (alpaca /
    # webull / ibkr) this stays NULL — `broker` itself is already accurate.
    brokerage_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # ── Listener gating ───────────────────────────────────────────────
    # Per-account knobs surfaced in the Brokers UI ("Auto Pull Orders"
    # + "Bring open orders" + "Bring Filled orders"). Govern what the
    # broker listener persists + fans out. Defaults are all-on so the
    # historic behaviour (mirror everything) is preserved for existing
    # rows after the migration.
    auto_pull_orders: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="true")
    bring_open_orders: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="true")
    bring_filled_orders: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="true")

    # Cached balance snapshot
    cash: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    buying_power: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    balance_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="broker_accounts")
    # No delete-orphan cascade — see Order.broker_account_id for the
    # rationale. Orders must survive their broker being disconnected so the
    # Performance / Order History audit trail stays intact. SET NULL at the
    # DB level handles the orphan transition; the Order row stays, just
    # without a broker pointer.
    orders = relationship("Order", back_populates="broker_account")
