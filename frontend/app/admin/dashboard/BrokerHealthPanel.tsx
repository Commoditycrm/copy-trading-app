"use client";

import { fmtDateTime, type BrokerHealth } from "./types";

function Chip({ label, value, tone = "muted" }: { label: string; value: number; tone?: "good" | "warn" | "bad" | "muted" }) {
  const color = tone === "good" ? "var(--good)" : tone === "warn" ? "#facc15" : tone === "bad" ? "var(--bad)" : "var(--text)";
  return (
    <div className="rounded-lg px-3 py-2" style={{ background: "var(--panel)", border: "1px solid var(--border)" }}>
      <span className="text-xs uppercase tracking-widest mr-2" style={{ color: "var(--muted)" }}>{label}</span>
      <span className="text-lg font-bold" style={{ color }}>{value}</span>
    </div>
  );
}

export function BrokerHealthPanel({ data, loading }: { data: BrokerHealth | null; loading: boolean }) {
  if (loading && !data) {
    return <div className="rounded-xl p-8 text-center text-sm" style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--muted)" }}>Loading broker health…</div>;
  }
  if (!data) return null;
  const { summary, accounts } = data;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-3">
        <Chip label="Connected" value={summary.connected} tone="good" />
        <Chip label="Problems" value={summary.problems} tone={summary.problems ? "bad" : "muted"} />
        <Chip label="Auto-pull off" value={summary.auto_pull_off} tone={summary.auto_pull_off ? "warn" : "muted"} />
        <Chip label="Total" value={summary.total} />
      </div>

      {accounts.length === 0 ? (
        <div className="rounded-xl p-6 text-sm" style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--muted)" }}>No broker accounts.</div>
      ) : (
        <div className="rounded-xl overflow-x-auto" style={{ border: "1px solid var(--border)" }}>
          <table className="w-full text-sm">
            <thead>
              <tr style={{ background: "rgba(255,255,255,0.03)", borderBottom: "1px solid var(--border)" }}>
                {["User", "Broker", "Status", "Auto-pull", "Last sync", "Error"].map((h) => (
                  <th key={h} className="px-3 py-3 text-left text-xs font-semibold" style={{ color: "var(--muted)" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {accounts.map((a, i) => (
                <tr key={i} style={{ borderBottom: "1px solid var(--border)", background: a.healthy ? undefined : "rgba(239,68,68,0.05)" }}>
                  <td className="px-3 py-2.5 text-xs" style={{ color: "var(--text-2)" }}>{a.user_name ?? a.user_email ?? "—"}</td>
                  <td className="px-3 py-2.5 text-xs">{a.broker}{a.is_paper ? <span style={{ color: "var(--muted)" }}> · paper</span> : null}</td>
                  <td className="px-3 py-2.5">
                    <span className="text-xs px-2 py-0.5 rounded-full font-medium" style={{
                      background: a.connection_status === "connected" ? "rgba(34,197,94,0.12)" : "rgba(239,68,68,0.12)",
                      color: a.connection_status === "connected" ? "#22c55e" : "#ef4444",
                    }}>{a.connection_status}</span>
                  </td>
                  <td className="px-3 py-2.5 text-xs" style={{ color: a.auto_pull_orders ? "var(--muted)" : "#facc15" }}>{a.auto_pull_orders ? "on" : "OFF"}</td>
                  <td className="px-3 py-2.5 text-xs" style={{ color: "var(--muted)" }}>{fmtDateTime(a.last_activity_sync_at)}</td>
                  <td className="px-3 py-2.5 text-xs" style={{ color: a.last_error ? "var(--bad)" : "var(--muted)" }}>{a.last_error ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
