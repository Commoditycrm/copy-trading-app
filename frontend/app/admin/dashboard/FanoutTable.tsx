"use client";

import { useState } from "react";
import { fmtDateTime, fmtTime, lagColor, type ChildOrder, type Fanout } from "./types";

const STATUS_COLORS: Record<string, { bg: string; color: string }> = {
  submitted:        { bg: "rgba(34,197,94,0.12)",  color: "#22c55e" },
  accepted:         { bg: "rgba(34,197,94,0.12)",  color: "#22c55e" },
  filled:           { bg: "rgba(34,197,94,0.18)",  color: "#16a34a" },
  partially_filled: { bg: "rgba(250,204,21,0.12)", color: "#facc15" },
  rejected:         { bg: "rgba(239,68,68,0.12)",  color: "#ef4444" },
  retry_pending:    { bg: "rgba(250,204,21,0.12)", color: "#facc15" },
  skipped_no_broker:{ bg: "rgba(148,163,184,0.12)",color: "#94a3b8" },
};

function StatusBadge({ status }: { status: string }) {
  const c = STATUS_COLORS[status] ?? { bg: "rgba(255,255,255,0.08)", color: "var(--text-2)" };
  return (
    <span className="text-xs px-2 py-0.5 rounded-full font-medium" style={{ background: c.bg, color: c.color }}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

function Lag({ v }: { v: number | null }) {
  if (v === null || v === undefined) return <span style={{ color: "var(--muted)" }}>—</span>;
  return <span style={{ color: lagColor(v), fontFamily: "monospace" }}>{v.toLocaleString()}ms</span>;
}

function Row({ fanout }: { fanout: Fanout }) {
  const [open, setOpen] = useState(false);
  const { total, submitted, errors } = fanout.subscribers;
  const successPct = total > 0 ? Math.round((submitted / total) * 100) : 0;

  return (
    <>
      <tr
        onClick={() => setOpen((o) => !o)}
        className="cursor-pointer"
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
        <td className="px-3 py-2.5 text-xs" style={{ color: "var(--text-2)" }}>
          {fanout.trader_display_name ?? fanout.trader_email ?? "—"}
        </td>
        <td className="px-3 py-2.5 text-xs" style={{ color: "var(--muted)" }}>{fmtDateTime(fanout.broker_accepted_at)}</td>
        <td className="px-3 py-2.5 text-xs">
          <span style={{ color: "#22c55e" }}>{submitted}</span>
          <span style={{ color: "var(--muted)" }}>/{total}</span>
          {errors > 0 && <span className="ml-1" style={{ color: "#ef4444" }}>({errors} err)</span>}
          <div className="mt-0.5 rounded-full overflow-hidden" style={{ height: 3, background: "var(--border)", width: 60 }}>
            <div style={{ width: `${successPct}%`, height: "100%", background: successPct === 100 ? "var(--good)" : "#facc15" }} />
          </div>
        </td>
        <td className="px-3 py-2.5"><Lag v={fanout.detection_lag_ms} /></td>
        <td className="px-3 py-2.5"><Lag v={fanout.fanout_duration_ms} /></td>
        <td className="px-3 py-2.5"><Lag v={fanout.total_ms} /></td>
      </tr>

      {open && fanout.children.map((child: ChildOrder) => (
        <tr key={child.order_id} style={{ background: "rgba(255,255,255,0.015)", borderBottom: "1px solid rgba(255,255,255,0.04)" }}>
          <td className="px-3 py-2" style={{ paddingLeft: 32 }}>
            <div className="text-xs" style={{ color: "var(--text-2)" }}>{child.subscriber_name ?? child.subscriber_email ?? "unknown"}</div>
            <div className="text-xs" style={{ color: "var(--muted)" }}>{child.broker_name ?? "—"}</div>
          </td>
          <td className="px-3 py-2"><StatusBadge status={child.status} /></td>
          <td className="px-3 py-2 text-xs" style={{ color: "var(--muted)" }}>{fmtTime(child.submitted_at)}</td>
          <td className="px-3 py-2 text-xs"><Lag v={child.pick_lag_ms} /></td>
          <td className="px-3 py-2 text-xs"><Lag v={child.broker_response_ms} /></td>
          <td className="px-3 py-2 text-xs"><Lag v={child.subscriber_lag_ms} /></td>
          <td className="px-3 py-2 text-xs">{child.reject_reason ? <span style={{ color: "var(--bad)" }}>{child.reject_reason}</span> : "—"}</td>
        </tr>
      ))}
    </>
  );
}

const HEADERS = ["Trade", "Trader", "Time", "Subscribers", "Detection", "Platform", "Total"];

export function FanoutTable({ fanouts, loading }: { fanouts: Fanout[] | null; loading: boolean }) {
  if (loading && !fanouts) {
    return <div className="rounded-xl p-8 text-center" style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--muted)" }}>Loading fan-outs…</div>;
  }
  if (!fanouts || fanouts.length === 0) {
    return (
      <div className="rounded-xl p-8 text-center" style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--muted)" }}>
        No fan-outs in this window. Try a wider range or a different trader.
      </div>
    );
  }
  return (
    <div className="rounded-xl overflow-x-auto" style={{ border: "1px solid var(--border)" }}>
      <table className="w-full text-sm">
        <thead>
          <tr style={{ background: "rgba(255,255,255,0.03)", borderBottom: "1px solid var(--border)" }}>
            {HEADERS.map((h) => (
              <th key={h} className="px-3 py-3 text-left text-xs font-semibold" style={{ color: "var(--muted)" }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {fanouts.map((f) => <Row key={f.parent_order_id} fanout={f} />)}
        </tbody>
      </table>
    </div>
  );
}
