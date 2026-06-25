"use client";

import { fmtMs, type Metrics } from "./types";

/** Median platform lag (our fan-out engine) vs median broker-call lag (the
 *  broker REST round-trip) on a shared scale — shows who owns the latency. */
export function PlatformBrokerSplit({ metrics, loading }: { metrics: Metrics | null; loading: boolean }) {
  const platform = metrics?.median_platform_ms ?? null;
  const broker = metrics?.median_broker_ms ?? null;
  const max = Math.max(1, platform ?? 0, broker ?? 0);

  const rows: { label: string; value: number | null; color: string; hint: string }[] = [
    { label: "Platform (fan-out engine)", value: platform, color: "var(--accent, #3b82f6)", hint: "detect → last subscriber accepted" },
    { label: "Broker call (REST round-trip)", value: broker, color: "#a855f7", hint: "our submit → broker accepted" },
  ];

  return (
    <div className="rounded-xl p-4 space-y-3" style={{ background: "var(--panel)", border: "1px solid var(--border)" }}>
      {loading && !metrics ? (
        <div className="text-center py-8 text-sm" style={{ color: "var(--muted)" }}>Loading…</div>
      ) : platform === null && broker === null ? (
        <div className="text-center py-8 text-sm" style={{ color: "var(--muted)" }}>No data in this window.</div>
      ) : (
        rows.map((r) => (
          <div key={r.label}>
            <div className="flex items-center justify-between text-xs mb-1">
              <span style={{ color: "var(--text-2)" }}>{r.label}</span>
              <span style={{ color: "var(--text)", fontFamily: "monospace" }}>{fmtMs(r.value)}</span>
            </div>
            <div className="rounded-full overflow-hidden" style={{ height: 8, background: "var(--bg-tint)" }}>
              <div style={{ width: `${((r.value ?? 0) / max) * 100}%`, height: "100%", background: r.color, transition: "width 0.3s" }} />
            </div>
            <div className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>{r.hint}</div>
          </div>
        ))
      )}
    </div>
  );
}
