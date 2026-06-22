"use client";

import { fmtMs, lagColor, type Fanout } from "./types";

const W = 820;
const H = 220;
const PAD = { top: 16, right: 16, bottom: 24, left: 48 };

/** Per-trade latency over the window: total lag (coloured dots + line) with the
 *  platform-only lag as a fainter underlay. Oldest → newest, left → right. */
export function LatencyTrend({ fanouts, loading }: { fanouts: Fanout[] | null; loading: boolean }) {
  // fanouts come newest-first; reverse for chronological x-axis.
  const series = (fanouts ?? [])
    .slice()
    .reverse()
    .map((f) => ({ total: f.total_ms, platform: f.fanout_duration_ms, symbol: f.symbol }))
    .filter((d) => d.total !== null || d.platform !== null);

  const inner = { w: W - PAD.left - PAD.right, h: H - PAD.top - PAD.bottom };
  const maxY = Math.max(1, ...series.map((d) => Math.max(d.total ?? 0, d.platform ?? 0)));
  const x = (i: number) => PAD.left + (series.length <= 1 ? inner.w / 2 : (i / (series.length - 1)) * inner.w);
  const y = (v: number) => PAD.top + inner.h - (v / maxY) * inner.h;

  const linePath = (key: "total" | "platform") =>
    series
      .map((d, i) => (d[key] === null ? null : `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(d[key]!).toFixed(1)}`))
      .filter(Boolean)
      .join(" ");

  // Threshold gridlines at 1.5s and 4s (the colour breakpoints).
  const gridLines = [1500, 4000].filter((g) => g <= maxY);

  return (
    <div className="rounded-xl p-4" style={{ background: "var(--panel)", border: "1px solid var(--border)" }}>
      {loading && !fanouts ? (
        <div className="text-center py-12 text-sm" style={{ color: "var(--muted)" }}>Loading latency…</div>
      ) : series.length === 0 ? (
        <div className="text-center py-12 text-sm" style={{ color: "var(--muted)" }}>No latency data in this window.</div>
      ) : (
        <>
          <svg viewBox={`0 0 ${W} ${H}`} width="100%" preserveAspectRatio="xMidYMid meet">
            {/* Y axis labels + baseline */}
            {[0, 0.5, 1].map((t) => {
              const val = maxY * t;
              const yy = y(val);
              return (
                <g key={t}>
                  <line x1={PAD.left} x2={W - PAD.right} y1={yy} y2={yy} stroke="var(--border)" strokeWidth={1} opacity={0.5} />
                  <text x={PAD.left - 6} y={yy + 3} textAnchor="end" fontSize={10} fill="var(--muted)">{fmtMs(Math.round(val))}</text>
                </g>
              );
            })}
            {gridLines.map((g) => (
              <line key={g} x1={PAD.left} x2={W - PAD.right} y1={y(g)} y2={y(g)}
                    stroke={lagColor(g)} strokeDasharray="3 3" strokeWidth={1} opacity={0.5} />
            ))}

            {/* Platform underlay */}
            <path d={linePath("platform")} fill="none" stroke="var(--muted)" strokeWidth={1.25} opacity={0.5} />
            {/* Total line */}
            <path d={linePath("total")} fill="none" stroke="var(--text-2)" strokeWidth={1.5} opacity={0.6} />
            {/* Coloured total dots */}
            {series.map((d, i) =>
              d.total === null ? null : (
                <circle key={i} cx={x(i)} cy={y(d.total)} r={2.6} fill={lagColor(d.total)}>
                  <title>{d.symbol}: {fmtMs(d.total)} total</title>
                </circle>
              ),
            )}
          </svg>
          <div className="flex items-center gap-4 mt-2 text-xs" style={{ color: "var(--muted)" }}>
            <span><span style={{ color: "var(--text-2)" }}>●</span> Total lag</span>
            <span><span style={{ color: "var(--muted)" }}>—</span> Platform lag</span>
            <span><span style={{ color: "var(--good)" }}>≤1.5s</span> · <span style={{ color: "#facc15" }}>≤4s</span> · <span style={{ color: "var(--bad)" }}>&gt;4s</span></span>
          </div>
        </>
      )}
    </div>
  );
}
