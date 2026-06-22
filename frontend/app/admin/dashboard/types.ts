// Shared types, filter model, and helpers for the admin Performance & Testing
// dashboard. Kept framework-free so both the data hook and the panels import it.

export interface ChildOrder {
  order_id: string;
  subscriber_email: string | null;
  subscriber_name: string | null;
  broker_name: string | null;
  status: string;
  quantity: string;
  filled_quantity: string;
  submitted_at: string | null;
  reject_reason: string | null;
  subscriber_lag_ms: number | null;
  pick_lag_ms: number | null;
  eligibility_lag_ms: number | null;
  broker_lag_ms: number | null;
  broker_response_ms: number | null;
  publish_lag_ms: number | null;
}

export interface Fanout {
  parent_order_id: string;
  trader_email: string | null;
  trader_display_name: string | null;
  symbol: string;
  side: string;
  quantity: string;
  instrument_type: string;
  broker_accepted_at: string | null;
  detected_at: string | null;
  fanout_completed_at: string | null;
  detection_lag_ms: number | null;
  fanout_duration_ms: number | null;
  total_ms: number | null;
  subscribers: { total: number; submitted: number; errors: number };
  children: ChildOrder[];
}

// Window-wide aggregates returned by GET /api/admin/performance/fanouts (M1).
export interface Metrics {
  fanouts_shown: number;
  trade_count: number;
  avg_fanout_ms: number | null;
  max_fanout_ms: number | null;
  median_platform_ms: number | null;
  median_broker_ms: number | null;
  success_rate: number | null;   // 0..1
  pct_within_1s: number | null;  // 0..1
  active_subscribers: number;
  truncated: boolean;
}

export interface PerfData {
  fanouts: Fanout[];
  metrics: Metrics;
}

// Per-broker leaderboard row from GET /api/admin/performance/by-broker (M1).
export interface BrokerStat {
  broker: string;
  accounts: number;
  mirrors: number;
  success_rate: number | null;
  median_detection_ms: number | null;
  median_broker_ms: number | null;
  median_subscriber_lag_ms: number | null;
}

export interface Trader {
  id: string;
  display_name: string | null;
  email: string;
  business_name: string | null;
}

// Latest test-suite result (M4) from GET /api/admin/tests/latest.
export interface TestSuiteResult {
  suite: string;
  passed: number;
  failed: number;
  skipped: number;
  duration_ms: number | null;
  source: string | null;
  commit_sha: string | null;
  created_at: string | null;
  pass_rate: number | null; // 0..1
}

// One load-test run (M4) from GET /api/admin/load-test/history.
export interface LoadTestRun {
  id: string;
  subscribers: number;
  total_ms: number | null;
  waves: number | null;
  errors: number;
  note: string | null;
  created_at: string | null;
}

// ── Filters ────────────────────────────────────────────────────────────────
export type RangeKey = "today" | "7d" | "30d" | "custom";

export interface Filters {
  traderId: string;   // "" = all traders
  range: RangeKey;
  from: string;       // datetime-local string, only used when range === "custom"
  to: string;
  broker: string;     // "" = all brokers (used by the latency panels, M3)
}

export const DEFAULT_FILTERS: Filters = {
  traderId: "",
  range: "today",
  from: "",
  to: "",
  broker: "",
};

/** Resolve the range control to concrete UTC ISO `from`/`to`. `now` is passed
 *  so callers can snapshot it once (avoids a moving window on every render). */
export function resolveRange(f: Filters, now: Date): { from: string | null; to: string | null } {
  if (f.range === "custom") {
    return {
      from: f.from ? new Date(f.from).toISOString() : null,
      to: f.to ? new Date(f.to).toISOString() : null,
    };
  }
  const to = now.toISOString();
  if (f.range === "today") {
    const start = new Date(now);
    start.setUTCHours(0, 0, 0, 0);
    return { from: start.toISOString(), to };
  }
  const days = f.range === "7d" ? 7 : 30;
  return { from: new Date(now.getTime() - days * 86_400_000).toISOString(), to };
}

/** Build the shared query string (trader + window) for the metric endpoints.
 *  `broker` is added by panels that want the broker-scoped view. */
export function buildQuery(f: Filters, now: Date, opts?: { broker?: boolean }): string {
  const { from, to } = resolveRange(f, now);
  const p = new URLSearchParams();
  if (f.traderId) p.set("trader_id", f.traderId);
  if (from) p.set("from", from);
  if (to) p.set("to", to);
  if (opts?.broker && f.broker) p.set("broker", f.broker);
  return p.toString();
}

// ── Display formatters (string-returning; JSX badges live in the panels) ─────
export function fmtMs(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v.toLocaleString()}ms`;
}

export function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${Math.round(v * 100)}%`;
}

export function fmtDateTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", {
    timeZone: "America/New_York",
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

export function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("en-US", {
    timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

/** Latency colour thresholds — green ≤1.5s / amber ≤4s / red >4s (per spec §4). */
export function lagColor(v: number | null | undefined): string {
  if (v === null || v === undefined) return "var(--muted)";
  return v <= 1500 ? "var(--good)" : v <= 4000 ? "#facc15" : "var(--bad)";
}
