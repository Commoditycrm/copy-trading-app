"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

// ── Types ─────────────────────────────────────────────────────────────────────
interface ChildOrder {
  order_id: string;
  subscriber_email: string | null;
  subscriber_name: string | null;
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
  broker_accepted_at: string | null;
  detected_at: string | null;
  fanout_completed_at: string | null;
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

// ── Expandable fanout row ──────────────────────────────────────────────────────
function FanoutRow({ fanout }: { fanout: Fanout }) {
  const [open, setOpen] = useState(false);
  const successRate = fanout.subscribers.total > 0
    ? Math.round((fanout.subscribers.submitted / fanout.subscribers.total) * 100)
    : 0;

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

        {/* Trader */}
        <td className="px-3 py-2.5 text-xs" style={{ color: "var(--text-2)" }}>
          {fanout.trader_display_name ?? fanout.trader_email ?? "—"}
          {fanout.trader_email && (
            <div className="text-xs" style={{ color: "var(--muted)" }}>{fanout.trader_email}</div>
          )}
        </td>

        {/* Time */}
        <td className="px-3 py-2.5 text-xs" style={{ color: "var(--muted)" }}>
          {fmtDate(fanout.broker_accepted_at)}
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

        {/* Timing */}
        <td className="px-3 py-2.5">{ms(fanout.detection_lag_ms)}</td>
        <td className="px-3 py-2.5">{ms(fanout.fanout_duration_ms)}</td>
        <td className="px-3 py-2.5">{ms(fanout.total_ms)}</td>
      </tr>

      {/* Expanded subscriber rows */}
      {open && fanout.children.map((child) => (
        <tr key={child.order_id} style={{ background: "rgba(255,255,255,0.015)", borderBottom: "1px solid rgba(255,255,255,0.04)" }}>
          <td className="px-3 py-2" style={{ paddingLeft: 32 }}>
            <div className="text-xs" style={{ color: "var(--text-2)" }}>
              {child.subscriber_name ?? child.subscriber_email ?? "unknown"}
            </div>
            <div className="text-xs" style={{ color: "var(--muted)" }}>{child.subscriber_email}</div>
          </td>
          <td className="px-3 py-2 text-xs" style={{ color: "var(--muted)" }}>subscriber</td>
          <td className="px-3 py-2 text-xs" style={{ color: "var(--muted)" }}>{fmt(child.submitted_at)}</td>
          <td className="px-3 py-2"><StatusBadge status={child.status} /></td>
          <td className="px-3 py-2 text-xs" style={{ color: "var(--muted)" }}>{ms(child.pick_lag_ms)}</td>
          <td className="px-3 py-2 text-xs">{ms(child.broker_lag_ms)}</td>
          <td className="px-3 py-2 text-xs">{ms(child.subscriber_lag_ms)}</td>
        </tr>
      ))}
    </>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────
export default function AdminPerformancePage() {
  const [data, setData]       = useState<PerfData | null>(null);
  const [loading, setLoading] = useState(true);
  const [limit, setLimit]     = useState(50);

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
        <div className="flex items-center gap-2">
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
            <div key={label} className="rounded-xl p-4" style={{ background: "rgba(14,20,17,0.6)", border: "1px solid var(--border)" }}>
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
        <div className="rounded-xl p-8 text-center" style={{ background: "rgba(14,20,17,0.5)", border: "1px solid var(--border)", color: "var(--muted)" }}>
          No fanout data yet. A trade must be placed and fanned out to subscribers first.
        </div>
      ) : (
        <div className="rounded-xl overflow-hidden" style={{ border: "1px solid var(--border)" }}>
          <table className="w-full text-sm">
            <thead>
              <tr style={{ background: "rgba(255,255,255,0.03)", borderBottom: "1px solid var(--border)" }}>
                {["Trade", "Trader", "Time", "Subscribers", "Detection Lag", "Fanout Duration", "Total Time"].map(h => (
                  <th key={h} className="px-3 py-3 text-left text-xs font-semibold" style={{ color: "var(--muted)" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.fanouts.map(f => <FanoutRow key={f.parent_order_id} fanout={f} />)}
            </tbody>
          </table>
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
