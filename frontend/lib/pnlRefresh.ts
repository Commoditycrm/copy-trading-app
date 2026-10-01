import type { BrokerAccount } from "@/lib/types";

// Safe fallback when no connected account exposes an interval (matches the
// backend default). Webull's 30s is the slowest, so it's the conservative floor.
export const DEFAULT_DAY_PNL_INTERVAL_MS = 30_000;

/** Effective steady refresh for the account Day P&L surfaces (calendar today
 *  cell + top card), in milliseconds. The value is the FASTEST interval the
 *  user's connected accounts ask for — a user on both Alpaca (10s) and Webull
 *  (30s) polls at 10s so neither lags. The per-broker number comes from broker
 *  capability metadata (`day_pnl_refresh_interval_s`), never a broker-name
 *  branch in the UI. */
export function dayPnlIntervalMs(accounts: BrokerAccount[] | null | undefined): number {
  const secs = (accounts ?? [])
    .filter((a) => a.connection_status === "connected")
    .map((a) => a.day_pnl_refresh_interval_s)
    .filter((n): n is number => typeof n === "number" && n > 0);
  return secs.length ? Math.min(...secs) * 1000 : DEFAULT_DAY_PNL_INTERVAL_MS;
}
