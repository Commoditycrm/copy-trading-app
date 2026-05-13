export type Role = "trader" | "subscriber";

export interface User {
  id: string;
  email: string;
  role: Role;
  display_name: string | null;
  is_active: boolean;
}

export interface BrokerAccount {
  id: string;
  broker: string;                  // free-form, returned by SnapTrade (e.g. "ALPACA", "SCHWAB")
  label: string;
  is_paper: boolean;
  supports_fractional: boolean;
  snaptrade_account_id: string;
  broker_account_number: string | null;
  connection_status: "pending" | "connected" | "error";
  last_error: string | null;
  created_at: string;

  cash: string | null;             // Decimal as string from API
  buying_power: string | null;
  total_equity: string | null;
  currency: string | null;
  balance_updated_at: string | null;
}

export interface SyncResult {
  added: number;
  removed: number;
  accounts: BrokerAccount[];
}

export type OrderSide = "buy" | "sell";
export type OrderType = "market" | "limit" | "stop" | "stop_limit";
export type OrderStatus =
  | "pending" | "submitted" | "accepted" | "partially_filled"
  | "filled" | "canceled" | "rejected" | "expired";
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
  broker_account_id: string;
  instrument_type: InstrumentType;
  symbol: string;
  side: OrderSide;
  order_type: OrderType;
  quantity: string;
  limit_price: string | null;
  stop_price: string | null;
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
  fills: Fill[];
}

export interface DailyPnL {
  day: string;
  realized_pnl: string;
  trade_count: number;
}

export interface SubscriberSettings {
  user_id: string;
  following_trader_id: string | null;
  copy_enabled: boolean;
  multiplier: string;
  daily_loss_limit: string | null;
  todays_realized_pnl: string | null;
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
