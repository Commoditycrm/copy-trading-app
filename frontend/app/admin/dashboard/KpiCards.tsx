"use client";

import { fmtMs, fmtPct, type Metrics } from "./types";

interface Props {
  metrics: Metrics | null;
  loading: boolean;
  /** Test pass % comes from the testing source (M4); null until wired. */
  testPassRate?: number | null;
}

interface Card {
  label: string;
  value: string;
  hint?: string;
  tone?: "good" | "warn" | "bad" | "muted";
}

function toneColor(t?: Card["tone"]) {
  switch (t) {
    case "good": return "var(--good)";
    case "warn": return "#facc15";
    case "bad": return "var(--bad)";
    default: return "var(--text)";
  }
}

function rateTone(v: number | null): Card["tone"] {
  if (v === null) return "muted";
  return v >= 0.95 ? "good" : v >= 0.8 ? "warn" : "bad";
}

export function KpiCards({ metrics, loading, testPassRate = null }: Props) {
  const cards: Card[] = metrics
    ? [
        { label: "Fanout success", value: fmtPct(metrics.success_rate), tone: rateTone(metrics.success_rate) },
        { label: "Median platform lag", value: fmtMs(metrics.median_platform_ms) },
        { label: "Subs within 1s", value: fmtPct(metrics.pct_within_1s), tone: rateTone(metrics.pct_within_1s) },
        { label: "Active subscribers", value: metrics.active_subscribers.toLocaleString() },
        {
          label: "Trades in range",
          value: metrics.trade_count.toLocaleString(),
          hint: metrics.truncated ? "capped at 2000" : undefined,
        },
        { label: "Test pass rate", value: fmtPct(testPassRate), tone: rateTone(testPassRate), hint: testPassRate === null ? "pending M4" : undefined },
      ]
    : [];

  if (loading && !metrics) {
    return (
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="rounded-xl p-4 animate-pulse" style={{ background: "var(--panel)", border: "1px solid var(--border)", height: 86 }} />
        ))}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
      {cards.map((c) => (
        <div key={c.label} className="rounded-xl p-4" style={{ background: "var(--panel)", border: "1px solid var(--border)" }}>
          <div className="text-xs uppercase tracking-widest mb-1" style={{ color: "var(--muted)" }}>{c.label}</div>
          <div className="text-2xl font-bold" style={{ color: toneColor(c.tone) }}>{c.value}</div>
          {c.hint && <div className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>{c.hint}</div>}
        </div>
      ))}
    </div>
  );
}
