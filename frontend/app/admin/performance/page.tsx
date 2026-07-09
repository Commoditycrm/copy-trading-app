"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

// ── Types ─────────────────────────────────────────────────────────────────────
interface ChildOrder {
  order_id: string;
  subscriber_email: string | null;
  subscriber_name: string | null;
  broker_name: string | null;
  status: string;
  quantity: string;
  filled_quantity: string;
  broker_order_id: string | null;
  submitted_at: string | null;
  reject_reason: string | null;
  subscriber_lag_ms: number | null;
  pick_lag_ms: number | null;
  eligibility_lag_ms: number | null;
  broker_lag_ms: number | null;
  publish_lag_ms: number | null;
}

interface Fanout {
  parent_order_id: string;
  trader_email: string | null;
  trader_display_name: string | null;
  symbol: string;
  side: string;
  quantity: string;
  instrument_type: string;
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
  if (v === null || v === undefined) return <span style={{ color: "var(--muted)" }}>—</span>;
  const color = v < 500 ? "var(--good)" : v < 2000 ? "#facc15" : "var(--bad)";
  return <span style={{ color, fontFamily: "monospace" }}>{v.toLocaleString()}ms</span>;
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

function fmtDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

const STATUS_COLORS: Record<string, { bg: string; color: string }> = {
  submitted:       { bg: "rgba(34,197,94,0.12)",   color: "#22c55e" },
  accepted:        { bg: "rgba(34,197,94,0.12)",   color: "#22c55e" },
  filled:          { bg: "rgba(34,197,94,0.18)",   color: "#16a34a" },
  partially_filled:{ bg: "rgba(250,204,21,0.12)",  color: "#facc15" },
  rejected:        { bg: "rgba(239,68,68,0.12)",   color: "#ef4444" },
  retry_pending:   { bg: "rgba(250,204,21,0.12)",  color: "#facc15" },
  skipped_no_broker: { bg: "rgba(148,163,184,0.12)", color: "#94a3b8" },
};

function StatusBadge({ status }: { status: string }) {
  const c = STATUS_COLORS[status] ?? { bg: "rgba(255,255,255,0.08)", color: "var(--text-2)" };
  return (
    <span className="text-xs px-2 py-0.5 rounded-full font-medium" style={{ background: c.bg, color: c.color }}>
      {status.replace(/_/g, " ")}
    </span>
  );
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
      className="px-3 py-3 text-left text-xs font-semibold cursor-pointer select-none"
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
        <td className="px-3 py-2.5">
          <span style={{ marginRight: 6, color: "var(--muted)", fontSize: 11 }}>{open ? "▾" : "▸"}</span>
          <span className="font-semibold">{fanout.symbol}</span>
          <span className="ml-2 text-xs" style={{ color: fanout.side === "buy" ? "#22c55e" : "#ef4444" }}>
            {fanout.side.toUpperCase()}
          </span>
          <span className="ml-1 text-xs" style={{ color: "var(--muted)" }}>×{fanout.quantity}</span>
        </td>

        {/* Instrument type */}
        <td className="px-3 py-2.5">
          <span className="text-xs px-2 py-0.5 rounded-full capitalize" style={{ background: "var(--panel-2)", color: "var(--text-2)" }}>
            {fanout.instrument_type}
          </span>
        </td>

        {/* Trader */}
        <td className="px-3 py-2.5 text-xs" style={{ color: "var(--text-2)" }}>
          {fanout.trader_display_name ?? fanout.trader_email ?? "—"}
          {fanout.trader_email && (
            <div className="text-xs" style={{ color: "var(--muted)" }}>{fanout.trader_email}</div>
          )}
        </td>

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

        {/* Subscribers */}
        <td className="px-3 py-2.5 text-xs">
          <span style={{ color: "#22c55e" }}>{fanout.subscribers.submitted}</span>
          <span style={{ color: "var(--muted)" }}>/{fanout.subscribers.total}</span>
          {fanout.subscribers.errors > 0 && (
            <span className="ml-1" style={{ color: "#ef4444" }}>({fanout.subscribers.errors} err)</span>
          )}
          <div className="mt-0.5 rounded-full overflow-hidden" style={{ height: 3, background: "var(--border)", width: 60 }}>
            <div style={{ width: `${successRate}%`, height: "100%", background: successRate === 100 ? "var(--good)" : "#facc15" }} />
          </div>
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
          <td colSpan={19} className="px-4 py-3">
            <div className="text-[10px] uppercase tracking-widest mb-2" style={{ color: "var(--muted)" }}>
              Per-subscriber breakdown ({fanout.children.length})
            </div>
            {fanout.children.length === 0 ? (
              <div className="text-xs" style={{ color: "var(--muted)" }}>No subscribers received this trade.</div>
            ) : (
              <div className="overflow-auto rounded" style={{ border: "1px solid var(--border)", maxHeight: "40vh" }}>
                <table className="w-full text-xs">
                  <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
                    <tr>
                      {["Subscriber", "Status", "Pick Lag", "Broker Lag", "Sub Lag"].map(h => (
                        <th key={h} className="text-left px-3 py-2 font-semibold" style={{ color: "var(--muted)" }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {fanout.children.map(child => (
                      <tr key={child.order_id} style={{ borderTop: "1px solid var(--border)" }}>
                        <td className="px-3 py-2">
                          <div style={{ color: "var(--text-2)" }}>{child.subscriber_name ?? child.subscriber_email ?? "unknown"}</div>
                          {child.subscriber_email && <div style={{ color: "var(--muted)" }}>{child.subscriber_email}</div>}
                        </td>
                        <td className="px-3 py-2"><StatusBadge status={child.status} /></td>
                        <td className="px-3 py-2">{ms(child.pick_lag_ms)}</td>
                        <td className="px-3 py-2">{ms(child.broker_lag_ms)}</td>
                        <td className="px-3 py-2">{ms(child.subscriber_lag_ms)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
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
                <PerfTh label="Type"             colKey="instrument"  sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <PerfTh label="Trader"           colKey="trader"      sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
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
        <div><span style={{ color: "var(--good)" }}>Green</span> = under 500ms · <span style={{ color: "#facc15" }}>Yellow</span> = 500ms–2s · <span style={{ color: "var(--bad)" }}>Red</span> = over 2s</div>
        <div>Detection Lag = time from broker accepting trader's order → our backend detecting it</div>
        <div>Fanout Duration = time from our backend detecting → last subscriber's broker accepting</div>
        <div>Total Time = broker accepted trader's order → last subscriber's broker accepted copy</div>
      </div>
    </div>
  );
}
