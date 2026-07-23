"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { ExportButton } from "@/components/ExportButton";
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

type PerfSortKey = "symbol" | "instrument" | "trader" | "time" | "subscribers" | "success" | "detection" | "fanout" | "total";

// Per-fanout mirror success ratio (submitted / total). -1 when no subscribers,
// so those sort to the bottom on a descending sort.
function successRatio(f: Fanout): number {
  return f.subscribers.total > 0 ? f.subscribers.submitted / f.subscribers.total : -1;
}

// Clickable header cell for the fanout table.
function PerfTh({
  label, colKey, sortKey, sortDir, onSort,
}: {
  label: string;
  colKey: PerfSortKey;
  sortKey: PerfSortKey;
  sortDir: "asc" | "desc";
  onSort: (k: PerfSortKey) => void;
}) {
  const active = sortKey === colKey;
  return (
    <th
      onClick={() => onSort(colKey)}
      className="px-3 py-3 text-left text-xs font-semibold cursor-pointer select-none whitespace-nowrap"
      style={{ color: active ? "var(--text-2)" : "var(--muted)" }}
      title={`Sort by ${label}`}
    >
      {label}
      <span style={{ marginLeft: 5, fontSize: 10, opacity: active ? 1 : 0.4 }}>
        {active ? (sortDir === "asc" ? "▲" : "▼") : "↕"}
      </span>
    </th>
  );
}

// Non-sortable header cell — for the timestamp/lag columns mirrored from the
// trader Performance table (sorting them by wall-clock adds little value).
function PlainTh({ label }: { label: string }) {
  return (
    <th className="px-3 py-3 text-left text-xs font-semibold whitespace-nowrap" style={{ color: "var(--muted)" }}>
      {label}
    </th>
  );
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
function FanoutRow({ fanout }: { fanout: Fanout }) {
  const [open, setOpen] = useState(false);
  const successRate = fanout.subscribers.total > 0
    ? Math.round((fanout.subscribers.submitted / fanout.subscribers.total) * 100)
    : 0;
  const blStats = brokerLagStats(fanout.children);

  return (
    <>
      {/* Parent row */}
      <tr
        onClick={() => setOpen(o => !o)}
        className="cursor-pointer transition-colors"
        style={{ borderBottom: "1px solid var(--border)" }}
        title="Click to see per-subscriber breakdown"
      >
        <td className="px-3 py-2.5 whitespace-nowrap">
          <span style={{ marginRight: 6, color: "var(--muted)", fontSize: 11 }}>{open ? "▾" : "▸"}</span>
          <span className="font-semibold">{fanoutSymbolLabel(fanout)}</span>
        </td>

        {/* Qty — Number() strips the trailing zeros from the Numeric(18,6)
            string ("3.000000" → "3"), while keeping real fractions ("2.5"). */}
        <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--text-2)" }}>{Number(fanout.quantity)}</td>

        {/* Side — buy (green) / sell (red) */}
        <td className="px-3 py-2.5">
          <span className="text-xs font-semibold uppercase" style={{ color: fanout.side === "buy" ? "#22c55e" : "#ef4444" }}>
            {fanout.side}
          </span>
        </td>

        {/* Order type — Market / Limit / Stop */}
        <td className="px-3 py-2.5 text-xs whitespace-nowrap" style={{ color: "var(--text-2)" }}>
          {orderTypeLabel(fanout.order_type)}
        </td>

        {/* Expected (limit) vs filled price */}
        <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtPrice(fanout.expected_price)}</td>
        <td className="px-3 py-2.5 text-xs tabular-nums">{fmtPrice(fanout.filled_avg_price)}</td>
        {/* Filled At — when the trader's order actually filled */}
        <td className="px-3 py-2.5 text-xs tabular-nums whitespace-nowrap" style={{ color: "var(--muted)" }}>{fmtClock(fanout.filled_at)}</td>

        {/* Trader */}
        <td className="px-3 py-2.5 text-xs" style={{ color: "var(--text-2)" }}>
          {fanout.trader_display_name ?? fanout.trader_email ?? "—"}
          {fanout.trader_email && (
            <div className="text-xs" style={{ color: "var(--muted)" }}>{fanout.trader_email}</div>
          )}
        </td>

        {/* Trade date (ET) — the timestamp columns show time-of-day only. */}
        <td className="px-3 py-2.5 text-xs tabular-nums whitespace-nowrap" style={{ color: "var(--muted)" }}>{fmtDate(fanout.broker_accepted_at ?? fanout.detected_at)}</td>

        {/* Timeline timestamps (HH:MM:SS.mmm ET) — mirrors the trader table. */}
        <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtClock(fanout.trader_submitted_at)}</td>
        <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtClock(fanout.broker_accepted_at)}</td>
        <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtClock(fanout.socket_received_at)}</td>
        <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtClock(fanout.detected_at)}</td>
        <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtClock(fanout.redis_published_at)}</td>
        <td className="px-3 py-2.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{fmtClock(fanout.fanout_completed_at)}</td>

        {/* Lags */}
        <td className="px-3 py-2.5">{ms(fanout.api_to_broker_lag_ms)}</td>
        <td className="px-3 py-2.5">{ms(fanout.publish_lag_ms)}</td>
        <td className="px-3 py-2.5">{ms(fanout.detection_lag_ms)}</td>
        <td className="px-3 py-2.5">{ms(fanout.fanout_duration_ms)}</td>
        <td className="px-3 py-2.5">{ms(fanout.total_ms)}</td>

        {/* Broker lag min / avg / max across subscriber children */}
        <td className="px-3 py-2.5 whitespace-nowrap">
          {ms(blStats.min)}
          {blStats.minBroker && <span className="ml-1.5 text-[10px]" style={{ color: "var(--muted)" }}>({blStats.minBroker})</span>}
        </td>
        <td className="px-3 py-2.5 whitespace-nowrap">
          {ms(blStats.avg)}
          {blStats.avgBroker && <span className="ml-1.5 text-[10px]" style={{ color: "var(--muted)" }}>({blStats.avgBroker})</span>}
        </td>
        <td className="px-3 py-2.5 whitespace-nowrap">
          {ms(blStats.max)}
          {blStats.maxBroker && <span className="ml-1.5 text-[10px]" style={{ color: "var(--muted)" }}>({blStats.maxBroker})</span>}
        </td>

        {/* Subscribers — same pill as the trader Performance table */}
        <td className="px-3 py-2.5">
          <SubscriberPill counts={fanout.subscribers} />
        </td>

        {/* Success rate */}
        <td className="px-3 py-2.5 text-xs font-medium" style={{
          color: fanout.subscribers.total === 0 ? "var(--muted)"
               : successRate === 100 ? "var(--good)"
               : successRate >= 50 ? "#facc15" : "var(--bad)",
        }}>
          {fanout.subscribers.total === 0 ? "—" : `${successRate}%`}
        </td>
      </tr>

      {/* Expanded: full-width per-subscriber drawer (trader-table pattern). */}
      {open && (
        <tr style={{ background: "var(--panel-2)" }}>
          <td colSpan={25} className="px-4 py-2.5">
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
  const [sortKey, setSortKey] = useState<PerfSortKey>("time");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  // The table filters q/side in the browser; the export is built server-side,
  // so pass them along or the file won't match what's on screen.
  function exportEndpoint() {
    const p = new URLSearchParams();
    if (q.trim()) p.set("search", q.trim());
    if (side !== "all") p.set("side", side);
    const qs = p.toString();
    return `/api/admin/performance/export${qs ? `?${qs}` : ""}`;
  }

  function toggleSort(k: PerfSortKey) {
    if (sortKey === k) setSortDir(d => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(k); setSortDir("asc"); }
  }

  async function load(lim = limit) {
    setLoading(true);
    try {
      const d = await api<PerfData>(`/api/admin/performance/fanouts?limit=${lim}`);
      setData(d);
    } catch (e) {
      notify.fromError(e, "Could not load performance data");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  // Null lags/times sort as -1 so missing values sink to the bottom on
  // a descending sort (where the interesting rows are).
  const num = (v: number | null) => (v ?? -1);
  const time = (iso: string | null) => (iso ? new Date(iso).getTime() : -1);

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
      switch (sortKey) {
        case "symbol":      return a.symbol.localeCompare(b.symbol) * dir;
        case "instrument":  return a.instrument_type.localeCompare(b.instrument_type) * dir;
        case "success":     return (successRatio(a) - successRatio(b)) * dir;
        case "trader":      return (a.trader_display_name ?? a.trader_email ?? "")
                                     .localeCompare(b.trader_display_name ?? b.trader_email ?? "") * dir;
        case "time":        return (time(a.broker_accepted_at) - time(b.broker_accepted_at)) * dir;
        case "subscribers": return (a.subscribers.total - b.subscribers.total) * dir;
        case "detection":   return (num(a.detection_lag_ms) - num(b.detection_lag_ms)) * dir;
        case "fanout":      return (num(a.fanout_duration_ms) - num(b.fanout_duration_ms)) * dir;
        case "total":       return (num(a.total_ms) - num(b.total_ms)) * dir;
        default:            return 0;
      }
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
          <ExportButton path={exportEndpoint()} label="Export" fallbackName="kopyya-fanouts.xlsx" />
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
                <PerfTh label="Trade"            colKey="symbol"      sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <PlainTh label="Qty" />
                <PlainTh label="Side" />
                <PlainTh label="Order Type" />
                <PlainTh label="Expected Price" />
                <PlainTh label="Filled Price" />
                <PlainTh label="Filled At" />
                <PerfTh label="Trader"           colKey="trader"      sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <PlainTh label="Date" />
                <PlainTh label="Trader Submitted At" />
                <PerfTh label="Broker Accepted At" colKey="time"      sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <PlainTh label="Trader Listened At" />
                <PlainTh label="DB Saved At" />
                <PlainTh label="Published For Subs At" />
                <PlainTh label="All Subs Completed At" />
                <PlainTh label="API→Broker" />
                <PlainTh label="UI Notification Lag" />
                <PerfTh label="Detection Lag"    colKey="detection"   sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <PerfTh label="Fanout Duration"  colKey="fanout"      sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <PerfTh label="Total Time"       colKey="total"       sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <PlainTh label="Lowest Broker Lag" />
                <PlainTh label="Average Broker Lag" />
                <PlainTh label="Highest Broker Lag" />
                <PerfTh label="Subscribers"      colKey="subscribers" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <PerfTh label="Success"          colKey="success"     sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
              </tr>
            </thead>
            <tbody>
              {visibleFanouts.map(f => <FanoutRow key={f.parent_order_id} fanout={f} />)}
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
