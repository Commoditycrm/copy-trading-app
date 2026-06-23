"use client";

import { fmtDateTime, fmtTime, type ListenerHealth } from "./types";

const STATE_COLORS: Record<string, { bg: string; color: string }> = {
  connected:          { bg: "rgba(34,197,94,0.12)",  color: "#22c55e" },
  connecting:         { bg: "rgba(250,204,21,0.12)", color: "#facc15" },
  reconnecting:       { bg: "rgba(250,204,21,0.12)", color: "#facc15" },
  disconnected:       { bg: "rgba(239,68,68,0.12)",  color: "#ef4444" },
  credentials_invalid:{ bg: "rgba(239,68,68,0.12)",  color: "#ef4444" },
  mfa_required:       { bg: "rgba(239,68,68,0.12)",  color: "#ef4444" },
};

function StateBadge({ state }: { state: string }) {
  const c = STATE_COLORS[state] ?? { bg: "rgba(255,255,255,0.08)", color: "var(--text-2)" };
  return (
    <span className="text-xs px-2 py-0.5 rounded-full font-medium" style={{ background: c.bg, color: c.color }}>
      {state?.replace(/_/g, " ") ?? "unknown"}
    </span>
  );
}

function Chip({ label, value, tone = "muted" }: { label: string; value: number; tone?: "good" | "bad" | "muted" }) {
  const color = tone === "good" ? "var(--good)" : tone === "bad" ? "var(--bad)" : "var(--text)";
  return (
    <div className="rounded-lg px-3 py-2" style={{ background: "var(--panel)", border: "1px solid var(--border)" }}>
      <span className="text-xs uppercase tracking-widest mr-2" style={{ color: "var(--muted)" }}>{label}</span>
      <span className="text-lg font-bold" style={{ color }}>{value}</span>
    </div>
  );
}

export function ListenerHealthPanel({ data, loading }: { data: ListenerHealth | null; loading: boolean }) {
  if (loading && !data) {
    return <div className="rounded-xl p-8 text-center text-sm" style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--muted)" }}>Loading listeners…</div>;
  }
  if (!data) return null;
  const { summary, listeners } = data;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-3">
        <Chip label="Connected" value={summary.connected} tone="good" />
        <Chip label="Down / degraded" value={summary.down} tone={summary.down ? "bad" : "muted"} />
        <Chip label="Total" value={summary.total} />
      </div>

      {listeners.length === 0 ? (
        <div className="rounded-xl p-6 text-sm" style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--muted)" }}>
          No active listeners. (If the worker is up, traders with a connected broker should appear here — the worker mirrors state to Redis.)
        </div>
      ) : (
        <div className="rounded-xl overflow-x-auto" style={{ border: "1px solid var(--border)" }}>
          <table className="w-full text-sm">
            <thead>
              <tr style={{ background: "rgba(255,255,255,0.03)", borderBottom: "1px solid var(--border)" }}>
                {["Trader", "State", "Last event", "Since", "Error"].map((h) => (
                  <th key={h} className="px-3 py-3 text-left text-xs font-semibold" style={{ color: "var(--muted)" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {listeners.map((l) => (
                <tr key={l.trader_id} style={{ borderBottom: "1px solid var(--border)", background: l.state === "connected" ? undefined : "rgba(239,68,68,0.05)" }}>
                  <td className="px-3 py-2.5 text-xs" style={{ color: "var(--text-2)" }}>{l.trader_name ?? l.trader_email ?? l.trader_id.slice(0, 8)}</td>
                  <td className="px-3 py-2.5"><StateBadge state={l.state} /></td>
                  <td className="px-3 py-2.5 text-xs" style={{ color: "var(--muted)" }}>{fmtTime(l.last_event_at)}</td>
                  <td className="px-3 py-2.5 text-xs" style={{ color: "var(--muted)" }}>{fmtDateTime(l.state_changed_at)}</td>
                  <td className="px-3 py-2.5 text-xs" style={{ color: l.last_error ? "var(--bad)" : "var(--muted)" }}>{l.last_error ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
