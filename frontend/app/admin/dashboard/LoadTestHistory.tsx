"use client";

import { fmtDateTime, fmtMs, type LoadTestRun } from "./types";

/** Recent load-test runs: subscribers, total time, broker waves, errors. */
export function LoadTestHistory({ data, loading }: { data: LoadTestRun[] | null; loading: boolean }) {
  if (loading && !data) {
    return <div className="rounded-xl p-8 text-center text-sm" style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--muted)" }}>Loading load-test history…</div>;
  }
  if (!data || data.length === 0) {
    return (
      <div className="rounded-xl p-6 text-sm" style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--muted)" }}>
        No load-test runs recorded yet. Have the load-test trigger POST to <code style={{ color: "var(--text-2)" }}>/api/admin/load-test/runs</code>.
      </div>
    );
  }

  return (
    <div className="rounded-xl overflow-x-auto" style={{ border: "1px solid var(--border)" }}>
      <table className="w-full text-sm">
        <thead>
          <tr style={{ background: "rgba(255,255,255,0.03)", borderBottom: "1px solid var(--border)" }}>
            {["When", "Subscribers", "Total time", "Waves", "Errors", "Note"].map((h) => (
              <th key={h} className="px-3 py-3 text-left text-xs font-semibold" style={{ color: "var(--muted)" }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((r) => (
            <tr key={r.id} style={{ borderBottom: "1px solid var(--border)" }}>
              <td className="px-3 py-2.5 text-xs" style={{ color: "var(--muted)" }}>{fmtDateTime(r.created_at)}</td>
              <td className="px-3 py-2.5 text-xs" style={{ color: "var(--text-2)" }}>{r.subscribers.toLocaleString()}</td>
              <td className="px-3 py-2.5 text-xs" style={{ fontFamily: "monospace" }}>{fmtMs(r.total_ms)}</td>
              <td className="px-3 py-2.5 text-xs" style={{ color: "var(--text-2)" }}>{r.waves ?? "—"}</td>
              <td className="px-3 py-2.5 text-xs" style={{ color: r.errors > 0 ? "var(--bad)" : "var(--muted)" }}>{r.errors}</td>
              <td className="px-3 py-2.5 text-xs" style={{ color: "var(--muted)" }}>{r.note ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
