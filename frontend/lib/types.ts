export type Role = "trader" | "subscriber" | "admin";

export interface User {
  id: string;
  email: string;
  role: Role;
  display_name: string | null;
  /** Trader-only brand / app name. Required at registration for traders;
   *  null for subscribers and admins. Shown as the app wordmark in the
   *  shell for the trader themselves and for any subscriber who follows
   *  them (see `SubscriberSettings.following_trader_business_name`). */
  business_name: string | null;
  is_active: boolean;
}

export type BrokerName = "alpaca" | "webull" | "snaptrade" | "ibkr";

export interface BrokerAccount {
  id: string;
  broker: BrokerName;
  label: string;
  is_paper: boolean;
  supports_fractional: boolean;
  broker_account_number: string | null;
  // Underlying broker for SnapTrade-routed accounts (e.g. "Webull",
  // "Robinhood", "IBKR"). null for direct-API brokers — `broker` itself
  // is already the real name in that case.
  brokerage_name?: string | null;
  connection_status: "pending" | "connected" | "error";
  last_error: string | null;
  created_at: string;

  cash: string | null;             // Decimal as string from API
  buying_power: string | null;
  total_equity: string | null;
  currency: string | null;
  balance_updated_at: string | null;

  // Listener-gating flags surfaced in the Brokers UI checkboxes.
  // PATCH /api/brokers/{id}/settings flips them.
  auto_pull_orders: boolean;
  bring_open_orders: boolean;
  bring_filled_orders: boolean;
}


export type OrderSide = "buy" | "sell";
export type OrderType = "market" | "limit" | "stop" | "stop_limit";
export type OrderStatus =
  | "pending" | "submitted" | "accepted" | "partially_filled"
  | "filled" | "canceled" | "rejected" | "expired"
  | "retry_pending";

/** Subscriber's wait-before-retry policy on transient broker errors.
 *  "never" = no retry, order fails immediately (pre-feature behaviour). */
export type RetryInterval = "never" | "1m" | "2m" | "3m" | "5m";
export type InstrumentType = "stock" | "option";
export type OptionRight = "call" | "put";

export interface Fill {
  quantity: string;
  price: string;
  fee: string;
  filled_at: string;
}

export interface Order {
  id: string;
  parent_order_id: string | null;
  // Nullable: orders survive when their broker is disconnected. See
  // backend/app/models/order.py for the rationale.
  broker_account_id: string | null;
  instrument_type: InstrumentType;
  symbol: string;
  side: OrderSide;
  order_type: OrderType;
  quantity: string;
  limit_price: string | null;
  stop_price: string | null;
  take_profit_price: string | null;
  stop_loss_price: string | null;
  option_expiry: string | null;
  option_strike: string | null;
  option_right: OptionRight | null;
  status: OrderStatus;
  broker_order_id: string | null;
  filled_quantity: string;
  filled_avg_price: string | null;
  submitted_at: string | null;
  closed_at: string | null;
  reject_reason: string | null;
  created_at: string;
  /** True when this order was broadcast to subscribers via copy fanout.
   *  False for subscribers' orders, trader orders placed while copy was
   *  paused, and trader orders placed with the "Just me" scope. */
  fanned_out_to_subscribers?: boolean;
  fills: Fill[];
}

export interface Position {
  broker_account_id: string;
  broker_symbol: string;              // canonical broker id; unique key for the position
  symbol: string;
  instrument_type: InstrumentType;
  quantity: string;                  // signed: positive = long, negative = short
  avg_entry_price: string | null;
  current_price: string | null;
  market_value: string | null;
  unrealized_pnl: string | null;
  cost_basis: string | null;
  option_expiry: string | null;
  option_strike: string | null;
  option_right: OptionRight | null;
}

export interface DailyPnL {
  day: string;
  realized_pnl: string;
  trade_count: number;
}

export interface SubscriberSettings {
  user_id: string;
  following_trader_id: string | null;
  /** Brand of the trader being followed — surfaced as the app wordmark
   *  in the AppShell so the subscriber sees the trader's app name. Null
   *  when not following anyone, or when following a legacy trader who
   *  pre-dates the business_name field. */
  following_trader_business_name?: string | null;
  copy_enabled: boolean;
  multiplier: string;
  daily_loss_limit: string | null;
  /** Daily realized-profit auto-pause. When today's realized P&L reaches
   *  this amount, copy_enabled flips to false. Auto-resumes next UTC day. */
  daily_profit_limit: string | null;
  /** Percentage variants of the daily loss / profit limits. Each is
   *  0 < x <= 100 and applied against `beginning_day_balance` to derive
   *  the dollar threshold every pnl_poller tick. UI uses these — the
   *  USD columns above are legacy. */
  daily_loss_limit_pct: string | null;
  daily_profit_limit_pct: string | null;
  todays_realized_pnl: string | null;
  /** Mirrors the followed trader's master pause. When true, the subscriber
   *  can't re-enable their own copy until the trader resumes. */
  trader_paused?: boolean;
  /** Retry policy for transient broker errors when *opening* a position. */
  retry_interval_open: RetryInterval;
  /** Retry policy for transient broker errors when *closing* a position. */
  retry_interval_close: RetryInterval;
  /** Subscriber's symbol denylist — trader trades on these symbols are
   *  NOT mirrored to this subscriber. Empty = no filter. Uppercase. */
  symbol_exclusion_list: string[];
  /** Subscriber's symbol allowlist — when non-empty, only trader trades
   *  on these symbols ARE mirrored. Empty = no filter (mirror all). */
  symbol_inclusion_list: string[];
  /** UI-only ceiling on per-contract dollar size. Persisted but NOT
   *  enforced server-side — the panel surfaces it for the user's own
   *  risk-tracking. */
  max_per_contract: string | null;
  /** Percent of today's beginning-day account balance (0–100). When
   *  today's filled trade NOTIONAL (USD) crosses
   *  -(beginning_day_balance * pct/100), pnl_poller auto-pauses copy. */
  max_account_pct_per_day: string | null;
  /** Auto-liquidation floor (USD). When broker-reported equity drops
   *  to/below this value, pnl_poller closes every open position at
   *  market and flips `copy_enabled` to false until the subscriber
   *  manually re-enables it. NULL disables the feature. */
  auto_liquidation_limit: string | null;
  /** Timestamp of the most recent auto-liquidation trigger (UTC ISO).
   *  Surfaced on the Settings page as "Auto-liquidated at …". Persists
   *  even after the subscriber clears the limit — it's an audit marker,
   *  not state. */
  auto_liquidated_at: string | null;
}

/** In-app notification (mirror retry failed, etc.). Persisted server-side
 *  for 30 days and dismissible via the inbox. */
export interface AppNotification {
  id: string;
  type: string;
  message: string;
  metadata: Record<string, unknown>;
  read_at: string | null;
  created_at: string;
}

export interface TraderSettings {
  user_id: string;
  trading_enabled: boolean;
}

export interface SubscriberSummary {
  user_id: string;
  email: string;
  display_name: string | null;
  copy_enabled: boolean;
  multiplier: string;
  broker_count: number;
  realized_pnl_30d: string;
}
