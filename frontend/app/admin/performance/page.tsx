"use client";

import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTableColumns, type ColumnDef, type ResolvedColumn, type TableColumns } from "@/lib/useTableColumns";
import { ColumnsMenu, ResizeHandle } from "@/components/ColumnsMenu";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { ExportDialog } from "./ExportDialog";
import { SubscriberPill, SubscriberBreakdown, type FanoutChild } from "@/components/performance/PerformanceView";

// ── Types ─────────────────────────────────────────────────────────────────────
// Both /api/performance/fanouts and /api/admin/performance/fanouts serialize
// children through the same backend helper, so reuse the trader view's type
// rather than maintaining a second copy that silently drifts.
type ChildOrder = FanoutChild;

interface Fanout {
  parent_order_id: string;
  trader_email: string | null;
  trader_display_name: string | null;
  symbol: string;
  side: string;
  quantity: string;
  instrument_type: string;
  order_type: string;
  status: string;
  option_expiry: string | null;
  option_strike: string | null;
  option_right: string | null;
  expected_price: string | null;
  filled_avg_price: string | null;
  filled_at: string | null;
  trader_submitted_at: string | null;
  broker_accepted_at: string | null;
  socket_received_at: string | null;
  detected_at: string | null;
  redis_published_at: string | null;
  fanout_completed_at: string | null;
  api_to_broker_lag_ms: number | null;
  detection_lag_ms: number | null;
  fanout_duration_ms: number | null;
  total_ms: number | null;
  publish_lag_ms: number | null;
  subscribers: { total: number; submitted: number; errors: number };
  children: ChildOrder[];
}

interface PerfData {
  fanouts: Fanout[];
  metrics: { fanouts_shown: number; avg_fanout_ms: number | null; max_fanout_ms: number | null };
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function ms(v: number | null) {
  if (v === null || v === undefined || v < 0) return <span style={{ color: "var(--muted)" }}>—</span>;
  // Format + color identically to the trader Performance panel (fmtMs/colorFor):
  // ms under 1s, centisecond-floored seconds under a minute, m/s above. Without
  // this the same trade read "1,567ms" here but "1.56s" on the trader panel.
  const color = v <= 1500 ? "var(--good)" : v <= 4000 ? "var(--warn)" : "var(--bad)";
  let text: string;
  if (v < 1000) text = `${v}ms`;
  else if (v < 60_000) text = `${(Math.floor(v / 10) / 100).toFixed(2)}s`;
  else { const ts = Math.floor(v / 1000); text = `${Math.floor(ts / 60)}m ${String(ts % 60).padStart(2, "0")}s`; }
  return <span style={{ color, fontFamily: "monospace" }}>{text}</span>;
}

// Short option expiry, matching the trader panel ("22 Jul 26").
function optionExpiryShort(isoDate: string): string {
  const d = new Date(isoDate.length === 10 ? isoDate + "T00:00:00Z" : isoDate);
  if (Number.isNaN(d.getTime())) return isoDate;
  const mon = d.toLocaleDateString("en-US", { month: "short", timeZone: "UTC" });
  return `${d.getUTCDate()} ${mon} ${String(d.getUTCFullYear()).slice(-2)}`;
}

// Full contract descriptor for the Trade column — same style as the trader
// panel: stock → "META"; option → "SPXW C $7510 22 Jul 26".
function fanoutSymbolLabel(f: Fanout): string {
  if (f.instrument_type !== "option") return f.symbol.toUpperCase();
  const cp = f.option_right === "call" ? "C" : f.option_right === "put" ? "P" : "";
  const strike = f.option_strike != null && f.option_strike !== "" ? `$${Number(f.option_strike)}` : "";
  const exp = f.option_expiry ? optionExpiryShort(f.option_expiry) : "";
  return [f.symbol.toUpperCase(), cp, strike, exp].filter(Boolean).join(" ");
}

// Raw order-type enum → display label for the Order Type column.
function orderTypeLabel(t: string): string {
  switch (t) {
    case "market": return "Market";
    case "limit": return "Limit";
    case "stop": return "Stop";
    case "stop_limit": return "Stop Limit";
    default: return t || "—";
  }
}

function fmt(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// HH:MM:SS.mmm (ET) — matches the trader Performance table's timestamp columns.
function fmtClock(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const t = d.toLocaleTimeString("en-US", {
    timeZone: "America/New_York", hourCycle: "h23",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  return `${t}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}

// Calendar date in US Eastern, e.g. "Jul 9, 2026" — the timestamp columns are
// time-only (fmtClock), so this is the only place the day is shown.
function fmtDate(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", { timeZone: "America/New_York", year: "numeric", month: "short", day: "numeric" });
}

// Price as $X.XX; "—" for null (market orders have no expected price).
function fmtPrice(p: string | null) {
  if (p === null || p === undefined || p === "") return "—";
  const n = Number(p);
  if (!Number.isFinite(n)) return "—";
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// Every column is sortable — the key IS the column id.
type PerfSortKey =
  | "symbol" | "qty" | "side" | "order_type" | "status"
  | "expected_price" | "filled_price" | "filled_at"
  | "trader" | "date" | "trader_submitted" | "broker_accepted"
  | "trader_listened" | "db_saved" | "published" | "all_subs"
  | "api_to_broker" | "ui_lag" | "detection" | "fanout_dur" | "total"
  | "lowest_bl" | "avg_bl" | "highest_bl" | "subscribers" | "success";

// Sort value for a fanout row by column. Strings compare case-insensitively;
// numbers/timestamps use -1 for missing so blanks sink on a descending sort.
function perfSortValue(f: Fanout, key: PerfSortKey): number | string {
  const t = (iso: string | null) => (iso ? new Date(iso).getTime() : -1);
  const n = (v: number | null) => (v ?? -1);
  const price = (v: string | null) => { const x = Number(v); return v != null && Number.isFinite(x) ? x : -1; };
  switch (key) {
    case "symbol":           return fanoutSymbolLabel(f).toUpperCase();
    case "qty":              return Number(f.quantity) || 0;
    case "side":             return f.side;
    case "order_type":       return f.order_type;
    case "status":           return f.status;
    case "expected_price":   return price(f.expected_price);
    case "filled_price":     return price(f.filled_avg_price);
    case "filled_at":        return t(f.filled_at);
    case "trader":           return (f.trader_display_name ?? f.trader_email ?? "").toUpperCase();
    case "date":             return t(f.broker_accepted_at ?? f.detected_at);
    case "trader_submitted": return t(f.trader_submitted_at);
    case "broker_accepted":  return t(f.broker_accepted_at);
    case "trader_listened":  return t(f.socket_received_at);
    case "db_saved":         return t(f.detected_at);
    case "published":        return t(f.redis_published_at);
    case "all_subs":         return t(f.fanout_completed_at);
    case "api_to_broker":    return n(f.api_to_broker_lag_ms);
    case "ui_lag":           return n(f.publish_lag_ms);
    case "detection":        return n(f.detection_lag_ms);
    case "fanout_dur":       return n(f.fanout_duration_ms);
    case "total":            return n(f.total_ms);
    case "lowest_bl":        return n(brokerLagStats(f.children).min);
    case "avg_bl":           return n(brokerLagStats(f.children).avg);
    case "highest_bl":       return n(brokerLagStats(f.children).max);
    case "subscribers":      return f.subscribers.total;
    case "success":          return successRatio(f);
  }
}

// Per-fanout mirror success ratio (submitted / total). -1 when no subscribers,
// so those sort to the bottom on a descending sort.
function successRatio(f: Fanout): number {
  return f.subscribers.total > 0 ? f.subscribers.submitted / f.subscribers.total : -1;
}

// Broker-lag min/avg/max across a fanout's subscriber children, with which
// broker hit the min/max. Mirrors the trader Performance table. avgBroker is
// only labelled when every contributing child shares one broker.
function brokerLagStats(children: ChildOrder[]): {
  min: number | null; minBroker: string | null;
  avg: number | null; avgBroker: string | null;
  max: number | null; maxBroker: string | null;
} {
  type Row = { ms: number; broker: string | null };
  const rows: Row[] = children
    .map(c => ({ ms: c.broker_lag_ms as number, broker: c.broker_name ?? null }))
    .filter((r): r is Row => typeof r.ms === "number" && Number.isFinite(r.ms) && r.ms >= 0);
  if (rows.length === 0) {
    return { min: null, minBroker: null, avg: null, avgBroker: null, max: null, maxBroker: null };
  }
  let minRow = rows[0], maxRow = rows[0], sum = 0;
  for (const r of rows) {
    if (r.ms < minRow.ms) minRow = r;
    if (r.ms > maxRow.ms) maxRow = r;
    sum += r.ms;
  }
  const distinct = new Set(rows.map(r => r.broker).filter(Boolean));
  return {
    min: minRow.ms, minBroker: minRow.broker,
    avg: Math.round(sum / rows.length),
    avgBroker: distinct.size === 1 ? Array.from(distinct)[0] : null,
    max: maxRow.ms, maxBroker: maxRow.broker,
  };
}

// ── Expandable fanout row ──────────────────────────────────────────────────────
function FanoutRow({ fanout, cols }: { fanout: Fanout; cols: TableColumns }) {
  const [open, setOpen] = useState(false);
  const successRate = fanout.subscribers.total > 0
    ? Math.round((fanout.subscribers.submitted / fanout.subscribers.total) * 100)
    : 0;
  const blStats = brokerLagStats(fanout.children);

  const cell: Record<string, ReactNode> = {
        symbol: (
        <td className="px-3 py-2.5 whitespace-nowrap">
          <span style={{ marginRight: 6, color: "var(--muted)", fontSize: 11 }}>{open ? "▾" : "▸"}</span>
          <span className="font-semibold">{fanoutSymbolLabel(fanout)}</span>
        </td>
        ),
        qty: <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--text-2)" }}>{Number(fanout.quantity)}</td>,
        side: (
        <td className="px-3 py-2.5">
          <span className="text-xs font-semibold uppercase" style={{ color: fanout.side === "buy" ? "#22c55e" : "#ef4444" }}>
            {fanout.side}
          </span>
        </td>
        ),
        order_type: (
        <td className="px-3 py-2.5 text-xs whitespace-nowrap" style={{ color: "var(--text-2)" }}>
          {orderTypeLabel(fanout.order_type)}
        </td>
        ),
        status: (
        <td className="px-3 py-2.5 text-xs whitespace-nowrap" style={{ color: "var(--text-2)", textTransform: "capitalize" }}>
          {(fanout.status || "").replace(/_/g, " ") || "—"}
        </td>
        ),
        expected_price: <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtPrice(fanout.expected_price)}</td>,
        filled_price: <td className="px-3 py-2.5 text-xs tabular-nums">{fmtPrice(fanout.filled_avg_price)}</td>,
        filled_at: <td className="px-3 py-2.5 text-xs tabular-nums whitespace-nowrap" style={{ color: "var(--muted)" }}>{fmtClock(fanout.filled_at)}</td>,
        trader: (
        <td className="px-3 py-2.5 text-xs" style={{ color: "var(--text-2)" }}>
          {fanout.trader_display_name ?? fanout.trader_email ?? "—"}
          {fanout.trader_email && (
            <div className="text-xs" style={{ color: "var(--muted)" }}>{fanout.trader_email}</div>
          )}
        </td>
        ),
        date: <td className="px-3 py-2.5 text-xs tabular-nums whitespace-nowrap" style={{ color: "var(--muted)" }}>{fmtDate(fanout.broker_accepted_at ?? fanout.detected_at)}</td>,
        trader_submitted: <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtClock(fanout.trader_submitted_at)}</td>,
        broker_accepted: <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtClock(fanout.broker_accepted_at)}</td>,
        trader_listened: <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtClock(fanout.socket_received_at)}</td>,
        db_saved: <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtClock(fanout.detected_at)}</td>,
        published: <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtClock(fanout.redis_published_at)}</td>,
        all_subs: <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtClock(fanout.fanout_completed_at)}</td>,
        api_to_broker: <td className="px-3 py-2.5">{ms(fanout.api_to_broker_lag_ms)}</td>,
        ui_lag: <td className="px-3 py-2.5">{ms(fanout.publish_lag_ms)}</td>,
        detection: <td className="px-3 py-2.5">{ms(fanout.detection_lag_ms)}</td>,
        fanout_dur: <td className="px-3 py-2.5">{ms(fanout.fanout_duration_ms)}</td>,
        total: <td className="px-3 py-2.5">{ms(fanout.total_ms)}</td>,
        lowest_bl: (
        <td className="px-3 py-2.5 whitespace-nowrap">
          {ms(blStats.min)}
          {blStats.minBroker && <span className="ml-1.5 text-[10px]" style={{ color: "var(--muted)" }}>({blStats.minBroker})</span>}
        </td>
        ),
        avg_bl: (
        <td className="px-3 py-2.5 whitespace-nowrap">
          {ms(blStats.avg)}
          {blStats.avgBroker && <span className="ml-1.5 text-[10px]" style={{ color: "var(--muted)" }}>({blStats.avgBroker})</span>}
        </td>
        ),
        highest_bl: (
        <td className="px-3 py-2.5 whitespace-nowrap">
          {ms(blStats.max)}
          {blStats.maxBroker && <span className="ml-1.5 text-[10px]" style={{ color: "var(--muted)" }}>({blStats.maxBroker})</span>}
        </td>
        ),
        subscribers: (
        <td className="px-3 py-2.5">
          <SubscriberPill counts={fanout.subscribers} />
        </td>
        ),
        success: (
        <td className="px-3 py-2.5 text-xs font-medium" style={{
          color: fanout.subscribers.total === 0 ? "var(--muted)"
               : successRate === 100 ? "var(--good)"
               : successRate >= 50 ? "#facc15" : "var(--bad)",
        }}>
          {fanout.subscribers.total === 0 ? "—" : `${successRate}%`}
        </td>
        ),
  };
  return (
    <>
      {/* Parent row */}
      <tr
        onClick={() => setOpen(o => !o)}
        className="cursor-pointer transition-colors"
        style={{ borderBottom: "1px solid var(--border)" }}
        title="Click to see per-subscriber breakdown"
      >
        {cols.columns.map((c) => <Fragment key={c.id}>{cell[c.id] ?? <td className="px-3 py-2.5" />}</Fragment>)}
      </tr>

      {/* Expanded: full-width per-subscriber drawer (trader-table pattern). */}
      {open && (
        <tr style={{ background: "var(--panel-2)" }}>
          <td colSpan={cols.columns.length} className="px-4 py-2.5">
            {/* Shared with the trader Performance view so admins see the exact
                same per-subscriber columns — no second copy to keep in sync. */}
            <SubscriberBreakdown mirrors={fanout.children} />
          </td>
        </tr>
      )}
    </>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────
export default function AdminPerformancePage() {
  const [data, setData]       = useState<PerfData | null>(null);
  const [loading, setLoading] = useState(true);
  const [limit, setLimit]     = useState(50);
  const [q, setQ]             = useState("");                              // search: symbol / trader
  const [side, setSide]       = useState<"all" | "buy" | "sell">("all");
  const [sortKey, setSortKey] = useState<PerfSortKey>("broker_accepted");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  // Configurable columns (per-user, synced). Trade (symbol) is locked — it holds
  // the expand caret. This table is 26 columns wide, so show/hide is the point.
  const columnDefs = useMemo<ColumnDef[]>(() => [
    { id: "symbol", header: "Trade", locked: true },
    { id: "qty", header: "Qty" },
    { id: "side", header: "Side" },
    { id: "order_type", header: "Order Type" },
    { id: "status", header: "Status" },
    { id: "expected_price", header: "Expected Price" },
    { id: "filled_price", header: "Filled Price" },
    { id: "filled_at", header: "Filled At" },
    { id: "trader", header: "Trader" },
    { id: "date", header: "Date" },
    { id: "trader_submitted", header: "Trader Submitted At" },
    { id: "broker_accepted", header: "Broker Accepted At" },
    { id: "trader_listened", header: "Trader Listened At" },
    { id: "db_saved", header: "DB Saved At" },
    { id: "published", header: "Published For Subs At" },
    { id: "all_subs", header: "All Subs Completed At" },
    { id: "api_to_broker", header: "API→Broker" },
    { id: "ui_lag", header: "UI Notification Lag" },
    { id: "detection", header: "Detection Lag" },
    { id: "fanout_dur", header: "Fanout Duration" },
    { id: "total", header: "Total Time" },
    { id: "lowest_bl", header: "Lowest Broker Lag" },
    { id: "avg_bl", header: "Average Broker Lag" },
    { id: "highest_bl", header: "Highest Broker Lag" },
    { id: "subscribers", header: "Subscribers" },
    { id: "success", header: "Success" },
  ], []);
  const cols = useTableColumns("admin_performance", columnDefs);
  function toggleSort(k: PerfSortKey) {
    if (sortKey === k) setSortDir(d => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(k); setSortDir("asc"); }
  }

  // Signature of the last rendered payload — SnapTrade-mirrored trades emit
  // order events in ~5s poll batches, each firing a background reload; only
  // touch state when the data actually changed so the table doesn't blink.
  const lastSigRef = useRef<string | null>(null);
  async function load(lim = limit, opts?: { background?: boolean }) {
    // Background (SSE-driven) reloads must NOT flip `loading` — that drops the
    // whole table to the "Loading…" state and back on every order event.
    if (!opts?.background) setLoading(true);
    try {
      const d = await api<PerfData>(`/api/admin/performance/fanouts?limit=${lim}`);
      const sig = JSON.stringify(d);
      if (sig !== lastSigRef.current) {
        lastSigRef.current = sig;
        setData(d);
      }
    } catch (e) {
      if (!opts?.background) notify.fromError(e, "Could not load performance data");
    } finally {
      if (!opts?.background) setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  // Live updates: refetch (debounced) on any order.* event so new fanouts
  // appear as trades happen — admins now receive platform-wide order events
  // via the global admin SSE channel. Background reload: no loading flash.
  const reloadTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEventStream((evt) => {
    if (!evt.type.startsWith("order.")) return;
    if (reloadTimer.current) clearTimeout(reloadTimer.current);
    reloadTimer.current = setTimeout(() => load(limit, { background: true }), 1000);
  });
  useEffect(() => () => { if (reloadTimer.current) clearTimeout(reloadTimer.current); }, []);

  const visibleFanouts = (data?.fanouts ?? [])
    .filter(f => {
      const needle = q.trim().toLowerCase();
      const matchQ = !needle ||
        f.symbol.toLowerCase().includes(needle) ||
        (f.trader_email ?? "").toLowerCase().includes(needle) ||
        (f.trader_display_name ?? "").toLowerCase().includes(needle);
      const matchSide = side === "all" || f.side === side;
      return matchQ && matchSide;
    })
    .sort((a, b) => {
      const dir = sortDir === "asc" ? 1 : -1;
      const va = perfSortValue(a, sortKey);
      const vb = perfSortValue(b, sortKey);
      const cmp = typeof va === "string" ? va.localeCompare(vb as string) : (va as number) - (vb as number);
      return cmp * dir;
    });

  const filtersActive = q.trim() !== "" || side !== "all";

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-xl font-bold">Performance — All Traders</h2>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            Every fanout across all traders. Click a row to expand subscriber-level breakdown.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* Search by symbol or trader */}
          <input
            type="text"
            placeholder="Filter by symbol or trader…"
            aria-label="Filter by symbol or trader"
            value={q}
            onChange={e => setQ(e.target.value)}
            className="text-sm px-3 py-1.5 rounded-lg"
            style={{
              background: "rgba(255,255,255,0.06)",
              border: "1px solid var(--border)",
              color: "var(--text)",
              outline: "none",
              minWidth: 200,
            }}
          />
          {/* Side filter */}
          <div className="flex gap-1">
            {(["all", "buy", "sell"] as const).map(s => (
              <button
                key={s}
                onClick={() => setSide(s)}
                className="text-xs px-3 py-1.5 rounded-lg capitalize font-medium transition-colors"
                style={{
                  background: side === s ? "var(--accent)" : "rgba(255,255,255,0.06)",
                  color:      side === s ? "var(--accent-ink)" : "var(--text-2)",
                  border:     "1px solid " + (side === s ? "var(--accent)" : "var(--border)"),
                }}
              >
                {s}
              </button>
            ))}
          </div>
          <select
            value={limit}
            onChange={e => { setLimit(+e.target.value); load(+e.target.value); }}
            aria-label="Number of rows to show"
            className="text-sm px-3 py-1.5 rounded-lg"
            style={{ background: "rgba(255,255,255,0.06)", border: "1px solid var(--border)", color: "var(--text)" }}
          >
            <option value={25}>Last 25</option>
            <option value={50}>Last 50</option>
            <option value={100}>Last 100</option>
            <option value={200}>Last 200</option>
          </select>
          <button
            onClick={() => load()}
            className="text-sm px-3 py-1.5 rounded-lg"
            style={{ background: "rgba(255,255,255,0.06)", border: "1px solid var(--border)", color: "var(--text-2)" }}
          >
            Refresh
          </button>
          {/* One row per subscriber mirror. Ignores the "Last N" selector on
              purpose — that bounds the on-screen table, not the export. */}
          <ExportDialog search={q} side={side} />
          {/* Show/hide + drag-reorder columns; drag a header edge to resize. */}
          <ColumnsMenu cols={cols} />
        </div>
      </div>

      {/* Summary metrics */}
      {data && (
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: "Fanouts shown",    value: data.metrics.fanouts_shown },
            { label: "Avg fanout time",  value: data.metrics.avg_fanout_ms != null ? `${data.metrics.avg_fanout_ms.toLocaleString()}ms` : "—" },
            { label: "Slowest fanout",   value: data.metrics.max_fanout_ms != null ? `${data.metrics.max_fanout_ms.toLocaleString()}ms` : "—" },
          ].map(({ label, value }) => (
            <div key={label} className="rounded-xl p-4" style={{ background: "var(--panel)", border: "1px solid var(--border)" }}>
              <div className="text-xs uppercase tracking-widest mb-1" style={{ color: "var(--muted)" }}>{label}</div>
              <div className="text-2xl font-bold">{value}</div>
            </div>
          ))}
        </div>
      )}

      {/* Table */}
      {loading ? (
        <div style={{ color: "var(--muted)" }}>Loading performance data…</div>
      ) : !data || data.fanouts.length === 0 ? (
        <div className="rounded-xl p-8 text-center" style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--muted)" }}>
          No fanout data yet. A trade must be placed and fanned out to subscribers first.
        </div>
      ) : visibleFanouts.length === 0 ? (
        <div className="rounded-xl p-8 text-center" style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--muted)" }}>
          No fanouts match your filter.
        </div>
      ) : (
        <div className="rounded-xl overflow-hidden" style={{ border: "1px solid var(--border)" }}>
          {filtersActive && (
            <div className="px-3 py-2 text-xs" style={{ background: "rgba(255,255,255,0.02)", color: "var(--muted)", borderBottom: "1px solid var(--border)" }}>
              Showing {visibleFanouts.length} of {data.fanouts.length} fanouts
            </div>
          )}
          <div className="overflow-auto" style={{ maxHeight: "70vh" }}>
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
              <tr style={{ background: "rgba(255,255,255,0.03)", borderBottom: "1px solid var(--border)" }}>
                {cols.columns.map((c) => {
                  const sk = c.id as PerfSortKey;   // every column is sortable
                  const active = sortKey === sk;
                  const w = c.width;
                  return (
                    <th
                      key={c.id}
                      onClick={() => toggleSort(sk)}
                      className="relative px-3 py-3 text-left text-xs font-semibold whitespace-nowrap cursor-pointer select-none"
                      style={{ color: active ? "var(--text-2)" : "var(--muted)", ...(w ? { width: w, minWidth: w, maxWidth: w } : {}) }}
                      title={`Sort by ${c.header}`}
                    >
                      {c.header}
                      <span style={{ marginLeft: 5, fontSize: 10, opacity: active ? 1 : 0.4 }}>{active ? (sortDir === "asc" ? "▲" : "▼") : "↕"}</span>
                      {!c.locked && <ResizeHandle minWidth={c.minWidth ?? 60} onResize={(px) => cols.setWidth(c.id, px)} />}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {visibleFanouts.map(f => <FanoutRow key={f.parent_order_id} fanout={f} cols={cols} />)}
            </tbody>
          </table>
          </div>
        </div>
      )}

      {/* Legend */}
      <div className="text-xs space-y-1 pt-2" style={{ color: "var(--muted)" }}>
        <div><span style={{ color: "var(--good)" }}>Green</span> = under 1.5s · <span style={{ color: "var(--warn)" }}>Yellow</span> = 1.5–4s · <span style={{ color: "var(--bad)" }}>Red</span> = over 4s</div>
        <div>Detection Lag = time from broker accepting trader's order → our backend detecting it</div>
        <div>Fanout Duration = time from our backend detecting → last subscriber's broker accepting</div>
        <div>Total Time = broker accepted trader's order → last subscriber's broker accepted copy</div>
      </div>
    </div>
  );
}
