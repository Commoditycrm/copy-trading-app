"use client";

import { fmtMs, fmtPct, lagColor, type BrokerStat } from "./types";

// §5.2 — architectural latency tiers. Static reference context for the measured
// numbers; the integration *type* sets a floor tuning can't remove.
const TIERS: { path: string; detection: string; placement: string; mirror: string; tier: string; tone: string }[] = [
  { path: "Alpaca (direct)", detection: "WebSocket push · <1s", placement: "REST ~35–50ms", mirror: "Yes — native bracket", tier: "Best", tone: "var(--good)" },
  { path: "Robinhood / Tradier · SnapTrade", detection: "polling · 5–60s", placement: "aggregator REST", mirror: "Yes — emulated bracket", tier: "Slow", tone: "#facc15" },
  { path: "Webull · SnapTrade", detection: "polling · 5–60s", placement: "aggregator REST", mirror: "Yes if not read-only", tier: "Slow", tone: "#facc15" },
  { path: "Some SnapTrade brokers", detection: "polling · 5–60s", placement: "—", mirror: "Read-only — can't mirror", tier: "N/A", tone: "var(--muted)" },
  { path: "Webull (direct)", detection: "—", placement: "—", mirror: "—", tier: "Removed from main", tone: "var(--bad)" },
  { path: "IBKR", detection: "stub", placement: "—", mirror: "—", tier: "Not prod-ready", tone: "var(--bad)" },
];

function isSnapTrade(label: string) {
  return /\(ST\)/i.test(label) || /snaptrade/i.test(label);
}

function Cell({ v }: { v: number | null }) {
  if (v === null || v === undefined) return <span style={{ color: "var(--muted)" }}>—</span>;
  return <span style={{ color: lagColor(v), fontFamily: "monospace" }}>{fmtMs(v)}</span>;
}

export function BrokerLeaderboard({ data, loading }: { data: BrokerStat[] | null; loading: boolean }) {
  return (
    <div className="space-y-4">
      {/* Measured leaderboard */}
      <div className="rounded-xl overflow-x-auto" style={{ border: "1px solid var(--border)" }}>
        <table className="w-full text-sm">
          <thead>
            <tr style={{ background: "rgba(255,255,255,0.03)", borderBottom: "1px solid var(--border)" }}>
              {["Broker", "Accounts", "Mirrors", "Success", "Detection", "Broker call", "End-to-end"].map((h) => (
                <th key={h} className="px-3 py-3 text-left text-xs font-semibold" style={{ color: "var(--muted)" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading && !data ? (
              <tr><td colSpan={7} className="px-3 py-8 text-center text-sm" style={{ color: "var(--muted)" }}>Loading…</td></tr>
            ) : !data || data.length === 0 ? (
              <tr><td colSpan={7} className="px-3 py-8 text-center text-sm" style={{ color: "var(--muted)" }}>No mirrors in this window.</td></tr>
            ) : (
              data.map((b) => (
                <tr key={b.broker} style={{ borderBottom: "1px solid var(--border)" }}>
                  <td className="px-3 py-2.5">
                    <span className="font-semibold">{b.broker}</span>
                    {isSnapTrade(b.broker) && (
                      <span className="ml-2 text-xs px-1.5 py-0.5 rounded" style={{ background: "rgba(250,204,21,0.12)", color: "#facc15" }} title="SnapTrade is poll-based — detection is bound to 5–60s upstream">
                        poll 5–60s
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-xs" style={{ color: "var(--text-2)" }}>{b.accounts}</td>
                  <td className="px-3 py-2.5 text-xs" style={{ color: "var(--text-2)" }}>{b.mirrors}</td>
                  <td className="px-3 py-2.5 text-xs">{fmtPct(b.success_rate)}</td>
                  <td className="px-3 py-2.5 text-xs"><Cell v={b.median_detection_ms} /></td>
                  <td className="px-3 py-2.5 text-xs"><Cell v={b.median_broker_ms} /></td>
                  <td className="px-3 py-2.5 text-xs"><Cell v={b.median_subscriber_lag_ms} /></td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* §5.2 Expected tiers — architectural reference */}
      <details className="rounded-xl" style={{ background: "var(--panel)", border: "1px solid var(--border)" }}>
        <summary className="px-4 py-3 cursor-pointer text-sm font-medium" style={{ color: "var(--text-2)" }}>
          Expected latency tiers (architectural reference)
        </summary>
        <div className="px-4 pb-4 overflow-x-auto">
          <p className="text-xs mb-3" style={{ color: "var(--muted)" }}>
            The integration <em>type</em> sets a latency floor tuning can&apos;t remove. Alpaca-direct is push-based
            (sub-second); anything via SnapTrade is detection-bound at 5–60s because SnapTrade polls the upstream broker.
          </p>
          <table className="w-full text-xs">
            <thead>
              <tr style={{ color: "var(--muted)", borderBottom: "1px solid var(--border)" }}>
                {["Broker path", "Detection", "Placement", "Mirror?", "Tier"].map((h) => (
                  <th key={h} className="px-2 py-2 text-left font-semibold">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {TIERS.map((t) => (
                <tr key={t.path} style={{ borderBottom: "1px solid rgba(255,255,255,0.04)" }}>
                  <td className="px-2 py-2" style={{ color: "var(--text-2)" }}>{t.path}</td>
                  <td className="px-2 py-2" style={{ color: "var(--muted)" }}>{t.detection}</td>
                  <td className="px-2 py-2" style={{ color: "var(--muted)" }}>{t.placement}</td>
                  <td className="px-2 py-2" style={{ color: "var(--muted)" }}>{t.mirror}</td>
                  <td className="px-2 py-2 font-medium" style={{ color: t.tone }}>{t.tier}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
