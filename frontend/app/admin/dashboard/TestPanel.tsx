"use client";

import { fmtDateTime, fmtMs, fmtPct, type TestSuiteResult } from "./types";

/** Pass/fail per test suite + last run time (latest row per suite). */
export function TestPanel({ data, loading }: { data: TestSuiteResult[] | null; loading: boolean }) {
  if (loading && !data) {
    return <div className="rounded-xl p-8 text-center text-sm" style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--muted)" }}>Loading tests…</div>;
  }
  if (!data || data.length === 0) {
    return (
      <div className="rounded-xl p-6 text-sm" style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--muted)" }}>
        No test results recorded yet. Have the test runner POST to <code style={{ color: "var(--text-2)" }}>/api/admin/tests/results</code> (or wire CI to it).
      </div>
    );
  }

  return (
    <div className="rounded-xl overflow-x-auto" style={{ border: "1px solid var(--border)" }}>
      <table className="w-full text-sm">
        <thead>
          <tr style={{ background: "rgba(255,255,255,0.03)", borderBottom: "1px solid var(--border)" }}>
            {["Suite", "Pass rate", "Passed", "Failed", "Skipped", "Duration", "Last run"].map((h) => (
              <th key={h} className="px-3 py-3 text-left text-xs font-semibold" style={{ color: "var(--muted)" }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((t) => {
            const ok = (t.failed ?? 0) === 0;
            return (
              <tr key={t.suite} style={{ borderBottom: "1px solid var(--border)" }}>
                <td className="px-3 py-2.5 font-medium">{t.suite}</td>
                <td className="px-3 py-2.5">
                  <span style={{ color: ok ? "var(--good)" : "var(--bad)", fontWeight: 600 }}>{fmtPct(t.pass_rate)}</span>
                </td>
                <td className="px-3 py-2.5 text-xs" style={{ color: "var(--good)" }}>{t.passed}</td>
                <td className="px-3 py-2.5 text-xs" style={{ color: t.failed > 0 ? "var(--bad)" : "var(--muted)" }}>{t.failed}</td>
                <td className="px-3 py-2.5 text-xs" style={{ color: "var(--muted)" }}>{t.skipped}</td>
                <td className="px-3 py-2.5 text-xs" style={{ color: "var(--muted)", fontFamily: "monospace" }}>{fmtMs(t.duration_ms)}</td>
                <td className="px-3 py-2.5 text-xs" style={{ color: "var(--muted)" }}>{fmtDateTime(t.created_at)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
