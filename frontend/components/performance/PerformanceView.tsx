"use client";

/**
 * Fanout Performance page (trader only).
 *
 * Shows latency breakdown of the trader's most recent fanouts:
 *  - Per-trade row: symbol/side/qty + broker_accepted_at / detected_at /
 *    fanout_completed_at and three derived durations (detection lag,
 *    fanout duration, total).
 *  - Click a row to expand into per-subscriber timing.
 *  - Auto-refreshes every 5s + on every order.* SSE event.
 */

import { Fragment, useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { api } from "@/lib/api";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";
import { PageLoading } from "@/components/PageLoading";

interface SubscriberCounts { total: number; submitted: number; errors: number; }
// Exported: the admin performance table renders the same rows from the same
// serializer, so it shares this type instead of re-declaring a subset.
export interface FanoutChild {
  order_id: string;
  subscriber_user_id: string;
  subscriber_email: string | null;
  subscriber_name: string | null;
  broker_name: string | null;
  status: string;
  quantity: string;
  filled_quantity: string;
  // The mirror's own expected (limit) vs actual fill price.
  expected_price: string | null;
  filled_avg_price: string | null;
  broker_order_id: string | null;
  submitted_at: string | null;
  created_at: string | null;
  reject_reason: string | null;
  subscriber_lag_ms: number | null;

  // New per-step lifecycle timestamps + lags (alembic e7a1d2c40f01).
  subscriber_picked_at: string | null;
  subscriber_accepted_at: string | null;
  broker_accepted_at: string | null;
  redis_published_at: string | null;
  pick_lag_ms: number | null;
  eligibility_lag_ms: number | null;
  broker_lag_ms: number | null;
  broker_response_ms: number | null;
  publish_lag_ms: number | null;
}
interface FanoutRow {
  parent_order_id: string;
  symbol: string;
  side: string;
  quantity: string;
  instrument_type: string;
  expected_price: string | null;
  filled_avg_price: string | null;
  broker_accepted_at: string | null;
  detected_at: string | null;
  fanout_completed_at: string | null;
  detection_lag_ms: number | null;
  fanout_duration_ms: number | null;
  total_ms: number | null;

  // New per-step lifecycle timestamps + lags.
  trader_submitted_at: string | null;
  socket_received_at: string | null;
  redis_published_at: string | null;
  api_to_broker_lag_ms: number | null;
  socket_lag_ms: number | null;
  publish_lag_ms: number | null;

  subscribers: SubscriberCounts;
  children: FanoutChild[];
}
interface FanoutMetrics {
  fanouts_shown: number;
  avg_fanout_ms: number | null;
  max_fanout_ms: number | null;
  avg_total_ms: number | null;
}
interface FanoutResponse { metrics: FanoutMetrics; fanouts: FanoutRow[]; }

// ── small formatters scoped to this page ───────────────────────────────

const MS_GOOD = 1500;       // ≤1.5s reads as healthy
const MS_WARN = 4000;       // 1.5-4s reads as warning; > red

function colorFor(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "var(--text)";
  if (ms <= MS_GOOD) return "var(--good)";
  if (ms <= MS_WARN) return "var(--warn)";
  return "var(--bad)";
}

function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 0) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) {
    // Floor to centiseconds (not round) so values like 59999ms don't
    // display as "60.00s" — which made it look like the >60s minutes
    // switch was broken. Now 59999ms → "59.99s" and only true ≥60s
    // values cross into the minutes formatter below.
    const cs = Math.floor(ms / 10);
    return `${(cs / 100).toFixed(2)}s`;
  }
  // ≥ 60s → minutes + seconds, e.g. "1m 05s", "2m 30s".
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

/** HH:MM:SS.mmm in US Eastern (America/New_York — auto EST/EDT). */
function fmtClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const t = d.toLocaleTimeString("en-US", {
    timeZone: "America/New_York",
    hourCycle: "h23",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const ms = String(d.getMilliseconds()).padStart(3, "0");
  return `${t}.${ms}`;
}

/** Calendar date in US Eastern, e.g. "Jul 9, 2026". Pairs with fmtClock, which
 *  is time-only — the timestamp columns show the clock, this shows the day. */
function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** Quantity without trailing-zero noise: "3.000000" → "3", "3.5" → "3.5". */
function fmtQty(q: string | number | null | undefined): string {
  if (q === null || q === undefined || q === "") return "—";
  const n = Number(q);
  if (!Number.isFinite(n)) return String(q);
  return n.toLocaleString(undefined, { maximumFractionDigits: 6 });
}

/** Price as $X.XX. "—" for null (e.g. market orders have no expected price). */
function fmtPrice(p: string | number | null | undefined): string {
  if (p === null || p === undefined || p === "") return "—";
  const n = Number(p);
  if (!Number.isFinite(n)) return "—";
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Min / mean / max of broker_lag_ms across a parent's subscriber mirrors,
 *  along with the broker name responsible for the min and max so the
 *  trader can see at a glance which broker is fastest / slowest. Rows
 *  with a null broker_lag_ms (mirror rejected before reaching the broker,
 *  or still in flight) are excluded from all three stats. Returns all-
 *  null when no child has a usable lag yet.
 *
 *  For "avg" we don't surface a single broker because it's an aggregate
 *  across many — but if every contributing row was on the *same* broker
 *  we surface that name (useful when the whole fanout went to one broker
 *  type, e.g. a fleet of Alpaca-only subscribers). */
function brokerLagStats(
  children: FanoutChild[]
): {
  min: number | null; minBroker: string | null;
  avg: number | null; avgBroker: string | null;
  max: number | null; maxBroker: string | null;
} {
  type Row = { ms: number; broker: string | null };
  const rows: Row[] = children
    .map(c => ({
      ms: c.broker_lag_ms as number,
      broker: c.broker_name ?? null,
    }))
    .filter((r): r is Row => typeof r.ms === "number" && Number.isFinite(r.ms) && r.ms >= 0);
  if (rows.length === 0) {
    return { min: null, minBroker: null, avg: null, avgBroker: null, max: null, maxBroker: null };
  }
  let minRow = rows[0], maxRow = rows[0], sum = 0;
  for (const r of rows) {
    if (r.ms < minRow.ms) minRow = r;
    if (r.ms > maxRow.ms) maxRow = r;
    sum += r.ms;
  }
  const distinctBrokers = new Set(rows.map(r => r.broker).filter(Boolean));
  return {
    min: minRow.ms, minBroker: minRow.broker,
    avg: Math.round(sum / rows.length),
    // Avg broker: only meaningful when every contributing row shares the
    // same broker name; otherwise the single label would be misleading.
    avgBroker: distinctBrokers.size === 1 ? Array.from(distinctBrokers)[0] : null,
    max: maxRow.ms, maxBroker: maxRow.broker,
  };
}

// ── Compact metric card with optional inline sparkline ────────────────

// ── Sort-direction chevron used by sortable inner-table headers ──────────
//
// Shows three states:
//   - inactive (active=false)        → both arrows dim, signals "clickable to sort"
//   - active asc (dir="asc")         → upper arrow highlighted
//   - active desc (dir="desc")       → lower arrow highlighted
// Pure SVG, no icon-lib dependency. The user-facing column header still
// owns the click + cursor:pointer; this is just the visual indicator.
function SortChevron({ dir, active }: { dir: "asc" | "desc" | undefined; active: boolean }) {
  const upColor = active && dir === "asc" ? "var(--accent)" : "rgba(255,255,255,0.25)";
  const downColor = active && dir === "desc" ? "var(--accent)" : "rgba(255,255,255,0.25)";
  return (
    <svg
      width="8"
      height="12"
      viewBox="0 0 8 12"
      aria-hidden
      style={{ flexShrink: 0 }}
    >
      <path d="M 4 0 L 8 4 L 0 4 Z" fill={upColor} />
      <path d="M 0 8 L 8 8 L 4 12 Z" fill={downColor} />
    </svg>
  );
}

function MetricCard({
  label, value, sub, valueColor, spark, Icon,
}: {
  label: string;
  value: string;
  sub?: string;
  valueColor?: string;
  spark?: number[];                 // numeric series for the inline sparkline
  Icon?: () => JSX.Element;         // small 14px icon shown next to the label
}) {
  return (
    <div
      className="rounded-lg px-3.5 py-3 flex flex-col"
      style={{
        background: "linear-gradient(180deg, rgba(14,20,17,0.7) 0%, rgba(7,9,10,0.4) 100%)",
        border: "1px solid var(--border)",
        minHeight: 88,
      }}
    >
      <div className="flex items-center justify-between gap-2 mb-1">
        <div
          className="flex items-center gap-1.5 text-[9px] uppercase tracking-widest"
          style={{ color: "var(--muted)" }}
        >
          {Icon && <Icon />}
          <span>{label}</span>
        </div>
        {spark && spark.length > 1 && (
          <Sparkline values={spark} color={valueColor || "var(--accent)"} />
        )}
      </div>
      <div
        className="leading-none"
        style={{ fontWeight: 600, fontSize: 22, color: valueColor || "var(--text)" }}
      >
        {value}
      </div>
      {sub && (
        <div className="text-[10px] mt-1.5" style={{ color: "var(--muted)" }}>
          {sub}
        </div>
      )}
    </div>
  );
}

// ── Inline SVG sparkline (~60×20px) ────────────────────────────────────

function Sparkline({ values, color }: { values: number[]; color: string }) {
  const w = 60, h = 20;
  const vals = values.filter(v => Number.isFinite(v));
  if (vals.length < 2) return null;
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const step = w / (vals.length - 1);
  const points = vals.map((v, i) => {
    const x = i * step;
    const y = h - ((v - min) / range) * (h - 4) - 2;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const path = `M ${points.join(" L ")}`;
  const area = `${path} L ${w},${h} L 0,${h} Z`;
  const gradId = `sp-${Math.random().toString(36).slice(2, 8)}`;
  return (
    <svg width={w} height={h} aria-hidden style={{ overflow: "visible" }}>
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.35" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gradId})`} />
      <path d={path} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ── Larger area chart for the trend panel (responsive width) ──────────

function LatencyAreaChart({
  values, height = 100, color = "var(--accent)",
}: { values: number[]; height?: number; color?: string }) {
  const w = 600;                    // SVG viewBox width; container scales it
  const padL = 32, padR = 8, padT = 8, padB = 18;
  const vals = values.filter(v => Number.isFinite(v));
  if (vals.length === 0) {
    return (
      <div
        className="grid place-items-center text-[11px]"
        style={{ height, color: "var(--muted)" }}
      >
        No data yet
      </div>
    );
  }
  const min = 0;
  const max = Math.max(...vals, 1000);
  const range = max - min || 1;
  const plotW = w - padL - padR;
  const plotH = height - padT - padB;
  const step = vals.length > 1 ? plotW / (vals.length - 1) : 0;
  const pts = vals.map((v, i) => {
    const x = padL + i * step;
    const y = padT + plotH - ((v - min) / range) * plotH;
    return [x, y] as const;
  });
  const linePath = `M ${pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" L ")}`;
  const areaPath = `${linePath} L ${pts[pts.length - 1][0].toFixed(1)},${padT + plotH} L ${pts[0][0].toFixed(1)},${padT + plotH} Z`;

  // Y-axis ticks at 0, mid, max
  const ticks = [0, max / 2, max];
  const gradId = `area-${Math.random().toString(36).slice(2, 8)}`;

  return (
    <svg viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" style={{ width: "100%", height }}>
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.35" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {/* Y grid lines + labels */}
      {ticks.map((t, i) => {
        const y = padT + plotH - ((t - min) / range) * plotH;
        return (
          <g key={i}>
            <line
              x1={padL} y1={y} x2={w - padR} y2={y}
              stroke="var(--border)" strokeDasharray="2 3" strokeWidth="0.5"
            />
            <text
              x={padL - 4} y={y + 3} textAnchor="end"
              fontSize="9" fill="var(--muted)"
            >
              {t < 1000 ? `${Math.round(t)}ms` : `${(t / 1000).toFixed(1)}s`}
            </text>
          </g>
        );
      })}
      <path d={areaPath} fill={`url(#${gradId})`} />
      <path d={linePath} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      {/* End-point dot for emphasis */}
      {pts.length > 0 && (
        <circle
          cx={pts[pts.length - 1][0]}
          cy={pts[pts.length - 1][1]}
          r="3"
          fill={color}
          stroke="var(--bg)"
          strokeWidth="1.5"
        />
      )}
    </svg>
  );
}

// ── Success / failure donut ────────────────────────────────────────────

function SuccessDonut({
  submitted, errors, skipped,
}: { submitted: number; errors: number; skipped: number }) {
  const total = submitted + errors + skipped;
  const size = 120;
  const cx = size / 2;
  const cy = size / 2;
  const r = 44;
  const stroke = 14;
  const circ = 2 * Math.PI * r;

  if (total === 0) {
    return (
      <div
        className="grid place-items-center text-[11px]"
        style={{ width: size, height: size, color: "var(--muted)" }}
      >
        No data
      </div>
    );
  }

  const pctSubmit = submitted / total;
  const pctError = errors / total;
  const pctSkip = skipped / total;

  // Stroke-dasharray trick — render three arcs by offsetting dashoffset.
  const arc = (frac: number, offset: number, color: string) => (
    <circle
      cx={cx} cy={cy} r={r}
      fill="none" stroke={color} strokeWidth={stroke}
      strokeDasharray={`${frac * circ} ${circ}`}
      strokeDashoffset={-offset * circ}
      transform={`rotate(-90 ${cx} ${cy})`}
      strokeLinecap="butt"
    />
  );

  const successPct = Math.round(pctSubmit * 100);

  return (
    <div className="flex items-center gap-4">
      <svg width={size} height={size}>
        {/* Track */}
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="var(--border)" strokeWidth={stroke} />
        {arc(pctSubmit, 0, "var(--good)")}
        {arc(pctError, pctSubmit, "var(--bad)")}
        {arc(pctSkip, pctSubmit + pctError, "var(--muted)")}
        <text
          x={cx} y={cy - 2} textAnchor="middle" dominantBaseline="middle"
          fontSize="22" fontWeight="600" fill="var(--text)"
        >
          {successPct}%
        </text>
        <text
          x={cx} y={cy + 14} textAnchor="middle" dominantBaseline="middle"
          fontSize="9" fill="var(--muted)" style={{ textTransform: "uppercase", letterSpacing: 1.5 }}
        >
          Success
        </text>
      </svg>
      <div className="space-y-1.5 text-xs">
        <LegendDot color="var(--good)" label="Submitted" value={submitted} />
        <LegendDot color="var(--bad)" label="Errors" value={errors} />
        <LegendDot color="var(--muted)" label="Skipped" value={skipped} />
      </div>
    </div>
  );
}

function LegendDot({ color, label, value }: { color: string; label: string; value: number }) {
  return (
    <div className="flex items-center gap-2">
      <span style={{ width: 8, height: 8, borderRadius: 2, background: color, display: "inline-block" }} />
      <span style={{ color: "var(--muted)", minWidth: 70 }}>{label}</span>
      <span className="tabular-nums" style={{ color: "var(--text)", fontWeight: 600 }}>{value}</span>
    </div>
  );
}

/**
 * Stacked mini-bar: 🟢 green = Platform lag, 🔵 blue = Broker lag.
 * Shows the proportional split between what the platform controls vs
 * what the broker (Alpaca) takes — purely visual, hover for exact ms.
 */
function PlatformBrokerBar({
  platformMs,
  brokerMs,
}: {
  platformMs: number | null;
  brokerMs: number | null;
}) {
  const p = platformMs ?? 0;
  const b = brokerMs ?? 0;
  const total = p + b;
  if (total === 0) return <span style={{ color: "var(--muted)", fontSize: 10 }}>—</span>;
  const platPct = Math.round((p / total) * 100);
  const brokerPct = 100 - platPct;
  return (
    <div className="flex flex-col gap-0.5">
      <div
        style={{
          display: "flex",
          width: 80,
          height: 5,
          borderRadius: 3,
          overflow: "hidden",
          background: "var(--border)",
        }}
        title={`Platform: ${p < 1000 ? `${p}ms` : `${(p / 1000).toFixed(2)}s`} · Broker: ${b < 1000 ? `${b}ms` : `${(b / 1000).toFixed(2)}s`}`}
      >
        <div style={{ width: `${platPct}%`, background: "var(--good)", height: "100%", transition: "width 200ms" }} />
        <div style={{ width: `${brokerPct}%`, background: "#3b82f6", height: "100%", transition: "width 200ms" }} />
      </div>
      <div className="flex justify-between tabular-nums" style={{ width: 80, fontSize: 9 }}>
        <span style={{ color: "var(--good)" }}>{fmtMs(p)}</span>
        <span style={{ color: "#3b82f6" }}>{fmtMs(b)}</span>
      </div>
    </div>
  );
}

// ── Horizontal bar chart for per-symbol latency ────────────────────────

function SymbolBars({ rows }: { rows: { symbol: string; avg_ms: number; count: number }[] }) {
  if (rows.length === 0) {
    return (
      <div className="grid place-items-center text-[11px] h-full" style={{ color: "var(--muted)" }}>
        No data
      </div>
    );
  }
  const max = Math.max(...rows.map(r => r.avg_ms), 1);
  return (
    <div className="space-y-2">
      {rows.map(r => {
        const pct = (r.avg_ms / max) * 100;
        const c = colorFor(r.avg_ms);
        return (
          <div key={r.symbol} className="flex items-center gap-2 text-xs">
            <div className="w-14 truncate font-medium" title={r.symbol}>{r.symbol}</div>
            <div
              className="flex-1 rounded overflow-hidden"
              style={{ height: 14, background: "rgba(255,255,255,0.04)" }}
            >
              <div
                style={{
                  width: `${pct}%`,
                  height: "100%",
                  background: `linear-gradient(90deg, ${c}40 0%, ${c} 100%)`,
                  transition: "width 200ms",
                }}
              />
            </div>
            <div className="w-16 text-right tabular-nums" style={{ color: c, fontWeight: 600 }}>
              {fmtMs(r.avg_ms)}
            </div>
            <div className="w-8 text-right tabular-nums text-[10px]" style={{ color: "var(--muted)" }}>
              ×{r.count}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Tiny icons ─────────────────────────────────────────────────────────

const IcoHash = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <line x1="4" y1="9" x2="20" y2="9" /><line x1="4" y1="15" x2="20" y2="15" />
    <line x1="10" y1="3" x2="8" y2="21" /><line x1="16" y1="3" x2="14" y2="21" />
  </svg>
);
const IcoClock = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
  </svg>
);
const IcoBolt = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
  </svg>
);
const IcoTarget = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <circle cx="12" cy="12" r="10" /><circle cx="12" cy="12" r="6" /><circle cx="12" cy="12" r="2" />
  </svg>
);

export function SubscriberPill({ counts }: { counts: { total: number; submitted: number; errors: number } }) {
  // "6 ✓ / 0 ✗ of 6" — green ok, red errors, neutral denominator.
  // `whitespace-nowrap` keeps the whole pill on a single line even when
  // the column is narrow; without it the column was breaking each
  // segment onto its own row (the screenshot fix).
  return (
    <span className="inline-flex items-center gap-1 text-xs whitespace-nowrap">
      <span style={{ color: "var(--good)" }}>{counts.submitted}&nbsp;✓</span>
      <span style={{ color: "var(--muted)" }}>/</span>
      <span style={{ color: counts.errors > 0 ? "var(--bad)" : "var(--muted)" }}>
        {counts.errors}&nbsp;✗
      </span>
      <span style={{ color: "var(--muted)" }}>of&nbsp;{counts.total}</span>
    </span>
  );
}

/**
 * Client-friendly per-trade summary shown above the per-subscriber table.
 *
 * Why this exists
 * ---------------
 * The parent row's `Total` column shows max(subscriber_lag) — one slow
 * subscriber can make a trade where 99% of mirrors landed in <1 s look
 * like it "took 15 seconds." That's accurate but easy to misread as a
 * platform-wide slowness. This card surfaces the *distribution* (p50,
 * % under 1 s) and names the slowest subscriber as a specific outlier
 * — so the reader sees both the typical experience and the worst case,
 * with attribution.
 *
 * Uses `subscriber_lag_ms` (parent detected → broker accepted) as the
 * per-subscriber latency, NOT `publish_lag_ms`. The latter is browser
 * notification lag and isn't part of the actual trade timing.
 */
function TradeSummaryCard({ mirrors }: { mirrors: FanoutChild[] }) {
  // Pull the per-subscriber trade latencies. Subscribers whose mirror
  // never reached the broker (rejected up front) have null lag — we
  // count them separately as "errored" rather than mixing them into
  // the latency distribution.
  const lags: number[] = [];
  const slowest: { ms: number; name: string | null } = { ms: -1, name: null };
  let errored = 0;

  for (const c of mirrors) {
    if (c.subscriber_lag_ms === null || c.subscriber_lag_ms === undefined) {
      errored += 1;
      continue;
    }
    lags.push(c.subscriber_lag_ms);
    if (c.subscriber_lag_ms > slowest.ms) {
      slowest.ms = c.subscriber_lag_ms;
      slowest.name = c.subscriber_name
        || (c.subscriber_email ? c.subscriber_email.split("@")[0] : null);
    }
  }

  
  const placedCount = lags.length;
  const under1s = lags.filter(l => l <= 1000).length;
  // Median: sort + pick middle. Skip when we have no samples.
  let median: number | null = null;
  if (lags.length > 0) {
    const sorted = [...lags].sort((a, b) => a - b);
    const mid = sorted.length >> 1;
    median = sorted.length % 2 === 0
      ? Math.round((sorted[mid - 1] + sorted[mid]) / 2)
      : sorted[mid];
  }

  // Headline: % under 1 s when most subs placed at all.
  const pctUnder1s = placedCount > 0
    ? Math.round((under1s / placedCount) * 100)
    : 0;

  // For the slowest line we try to attribute the cause: an errored
  // subscriber didn't pick a broker call at all (likely a rejection),
  // so it's not a "slow Alpaca call" — distinguish.
  const slowestCause = slowest.ms >= 0
    ? (slowest.ms >= 5000
        ? "broker account slow / rate-limited"
        : slowest.ms >= 1000
          ? "broker call slow"
          : "normal")
    : "";

  // ── Platform vs Broker split ────────────────────────────────────────────────
  // Platform Lag = pick_lag + eligibility_lag (steps we own: queue + gate checks).
  // Broker Lag   = broker_lag (the broker's REST call round-trip — external).
  // Only include mirrors that actually reached the broker (subscriber_lag_ms set).
  const platformLags: number[] = [];
  const brokerLagsSplit: number[] = [];
  for (const c of mirrors) {
    if (c.subscriber_lag_ms === null || c.subscriber_lag_ms === undefined) continue;
    platformLags.push((c.pick_lag_ms ?? 0) + (c.eligibility_lag_ms ?? 0));
    if (c.broker_lag_ms !== null && c.broker_lag_ms !== undefined) {
      brokerLagsSplit.push(c.broker_lag_ms);
    }
  }

  function medianOf(arr: number[]): number | null {
    if (arr.length === 0) return null;
    const s = [...arr].sort((a, b) => a - b);
    const mid = s.length >> 1;
    return s.length % 2 === 0 ? Math.round((s[mid - 1] + s[mid]) / 2) : s[mid];
  }

  const medPlatform = medianOf(platformLags);
  const medBroker   = medianOf(brokerLagsSplit);

  return (
    <div
      className="mb-3 rounded-lg border px-4 py-3"
      style={{
        borderColor: "var(--border)",
        background: "linear-gradient(180deg, rgba(34,197,94,0.06) 0%, rgba(0,0,0,0) 100%)",
      }}
    >
      <div className="text-[10px] uppercase tracking-widest mb-2" style={{ color: "var(--muted)" }}>
        Trade summary
      </div>
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1 text-sm">
        {/* Headline: how many subs got placed quickly. */}
        <div>
          <span style={{ color: pctUnder1s >= 90 ? "var(--good)" : "var(--warn)", fontWeight: 600 }}>
            {under1s} of {placedCount}
          </span>
          <span style={{ color: "var(--muted)" }}> subscribers placed within 1 second</span>
          {placedCount > 0 && (
            <span style={{ color: "var(--muted)" }}> ({pctUnder1s}%)</span>
          )}
        </div>
        {/* Median latency — the "typical" subscriber experience. */}
        {median !== null && (
          <div>
            <span style={{ color: "var(--muted)" }}>Median: </span>
            <span style={{ color: colorFor(median), fontWeight: 600 }}>{fmtMs(median)}</span>
          </div>
        )}
        {/* Slowest as a named outlier with attribution. */}
        {slowest.ms >= 0 && (
          <div>
            <span style={{ color: "var(--muted)" }}>Slowest: </span>
            <span style={{ color: colorFor(slowest.ms), fontWeight: 600 }}>
              {fmtMs(slowest.ms)}
            </span>
            {slowest.name && (
              <span style={{ color: "var(--muted)" }}> ({slowest.name}{slowestCause !== "normal" ? ` — ${slowestCause}` : ""})</span>
            )}
          </div>
        )}
        {/* Errors (credentials, etc.) — separated from latency stats. */}
        {errored > 0 && (
          <div>
            <span style={{ color: "var(--bad)", fontWeight: 600 }}>{errored} errored</span>
            <span style={{ color: "var(--muted)" }}> (e.g. credential issues — see Reject Reason)</span>
          </div>
        )}
      </div>
      {/* ── Platform vs Broker split ──────────────────────────────────── */}
      {medPlatform !== null && medBroker !== null && (
        <div className="mt-3 pt-3" style={{ borderTop: "1px solid var(--border)" }}>
          <div className="text-[10px] uppercase tracking-widest mb-2" style={{ color: "var(--muted)" }}>
            Median Lag Split
          </div>
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
            {/* Total */}
            {median !== null && (
              <div className="flex items-baseline gap-1 text-sm">
                <span style={{ color: "var(--muted)" }}>Total:</span>
                <span style={{ color: colorFor(median), fontWeight: 600 }}>{fmtMs(median)}</span>
              </div>
            )}
            {/* Platform */}
            <div className="flex items-center gap-1.5 text-sm">
              <span style={{ color: "var(--muted)" }}>Platform:</span>
              <span style={{ color: colorFor(medPlatform), fontWeight: 600 }}>{fmtMs(medPlatform)}</span>
            </div>
            {/* Broker */}
            <div className="flex items-center gap-1.5 text-sm">
              <span style={{ color: "var(--muted)" }}>Broker:</span>
              <span style={{ color: colorFor(medBroker), fontWeight: 600 }}>{fmtMs(medBroker)}</span>
            </div>
            {/* Stacked bar */}
            {/* <PlatformBrokerBar platformMs={medPlatform} brokerMs={medBroker} /> */}
          </div>
        </div>
      )}

      <div className="mt-2 text-[11px]" style={{ color: "var(--muted)" }}>
        Note: per-subscriber timings below show <b>trade latency</b> (Subscriber Lag). The
        separate <b>UI Notification Lag</b> column is when the subscriber&apos;s browser
        received the SSE update — independent of when their order was actually placed.
      </div>
    </div>
  );
}

// Per-subscriber inner-table date/time columns the user can sort by.
// Order here is just for type safety — the actual column order in the UI
// is determined by the headers array further down.
type ChildSortField =
  | "created_at"
  | "subscriber_picked_at"
  | "subscriber_accepted_at"
  | "broker_accepted_at"
  | "redis_published_at";
type SortDir = "asc" | "desc";
interface ChildSort { field: ChildSortField; dir: SortDir; }

/** Per-subscriber breakdown — the trade summary card plus the full mirror
 *  timeline. Exported so the admin performance table renders the SAME columns
 *  as the trader Performance view instead of a parallel copy. Owns its own
 *  sort state, so each open drawer sorts independently. */
export function SubscriberBreakdown({ mirrors }: { mirrors: FanoutChild[] }) {
  const [sort, setSort] = useState<ChildSort | null>(null);

  // unsorted -> asc -> desc -> unsorted, so the reader can get back to
  // chronological order without collapsing the row.
  function cycleSort(field: ChildSortField) {
    setSort((cur) => {
      if (!cur || cur.field !== field) return { field, dir: "asc" };
      if (cur.dir === "asc") return { field, dir: "desc" };
      return null;
    });
  }

  // Null timestamps sort LAST either way — a mirror that never reached
  // "Broker Accepted At" shouldn't shove real rows around.
  function applyChildSort(children: FanoutChild[]): FanoutChild[] {
    if (!sort) return children;
    const sorted = [...children];
    sorted.sort((a, b) => {
      const av = (a as unknown as Record<ChildSortField, string | null>)[sort.field];
      const bv = (b as unknown as Record<ChildSortField, string | null>)[sort.field];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return sorted;
  }

  return (
    <>
                          {/* Headline summary — the client-friendly framing.
                              Avoids the "trade took 15.9s" misread by showing
                              what most subscribers actually experienced (p50,
                              # placed within 1s) and naming the slowest as a
                              named outlier rather than a platform stat. */}
                          {mirrors.length > 0 && <TradeSummaryCard mirrors={mirrors} />}
                          <div
                            className="text-[10px] uppercase tracking-widest mb-3"
                            style={{ color: "var(--muted)" }}
                          >
                            Per-Subscriber Timeline ({mirrors.length} target{mirrors.length === 1 ? "" : "s"})
                          </div>
                          {mirrors.length === 0 ? (
                            <div className="text-xs" style={{ color: "var(--muted)" }}>
                              No subscribers received this trade.
                            </div>
                          ) : (
                            // Scroll wrapper so the inner per-subscriber table
                            // gets the same sticky-header treatment as the
                            // outer fanout table. max-h caps the drawer's own
                            // height (already nested inside the page-level
                            // scrolling container).
                            <div
                              className="overflow-auto max-h-[50vh] rounded"
                              style={{ border: "1px solid var(--border)" }}
                            >
                            <table
                              className="w-full text-xs"
                              style={{ borderCollapse: "separate", borderSpacing: 0, tableLayout: "auto" }}
                            >
                              {/* Sticky thead pinned to the wrapper. Opaque
                                  panel background prevents row text from
                                  bleeding through behind the sticky row. */}
                              <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
                                <tr style={{ color: "var(--muted)" }}>
                                  {/* Per-column header definitions. The optional third element is the
                                      sort field — when present, the column renders a clickable sort
                                      indicator that cycles unsorted → asc → desc → unsorted. */}
                                  {([
                                    ["Subscriber", "The subscriber whose account this mirror was placed on."],
                                    ["Status", "Current state of this mirror order (PENDING / SUBMITTED / FILLED / REJECTED / RETRY_PENDING / etc)."],
                                    ["Qty", "Mirror quantity — trader's qty × this subscriber's multiplier, rounded per broker rules (floored to whole shares unless the broker supports fractional)."],
                                    ["Filled Qty", "Quantity actually filled by the subscriber's broker. Less than Qty means a partial fill."],
                                    ["Expected Price", "This mirror's limit price. Blank for market orders (no expected price)."],
                                    ["Filled Price", "The subscriber's broker average fill price for this mirror. Compare with Expected Price to gauge their slippage."],
                                    ["Created At", "When we inserted this subscriber's child Order row in our database (status=PENDING).", "created_at"],
                                    ["Picked At", "When copy_engine started processing this specific subscriber — the per-subscriber starting line.", "subscriber_picked_at"],
                                    ["Submitted to Broker", "When this subscriber passed every eligibility check (daily-loss limit not hit, copy still enabled, broker available, scaled qty > 0). We're about to call their broker.", "subscriber_accepted_at"],
                                    ["Broker Accepted At", "When this subscriber's broker (Alpaca) confirmed acceptance of the mirror order.", "broker_accepted_at"],
                                    ["Published to UI", "When we broadcast the mirror's outcome via SSE so the subscriber's open tabs update in real time.", "redis_published_at"],
                                    ["Pick Lag", "Platform-owned. Parent detected → this subscriber picked. Picked At − parent DB SAVED AT. Grows with the number of subscribers ahead of this one in the fanout queue."],
                                    ["Eligibility Lag", "Platform-owned. Picked → ready to call broker. Submitted to Broker − Picked At. Time spent on gate checks (daily-loss P&L lookup, settings reads)."],
                                    ["Broker Name", "The subscriber's connected broker that this mirror order was placed on."],
                                    ["Broker Lag", "Broker-owned (external). Submit → broker accepted. Broker Accepted At − Accepted At. The single broker REST call's round-trip — outside platform control."],
                                    ["Broker Response", "Broker-owned (external). How long the broker's place-order call took to return ANY response — success or error. Measured around the SDK call itself."],
                                    // ["Split", "Visual split: 🟢 green = Platform lag (pick + eligibility) · 🔵 blue = Broker lag (Alpaca round-trip). Hover for exact ms."],
                                    ["UI Notification Lag", "Broker accept → SSE pushed to subscriber's browser. Published to UI − Broker Accepted At. NOTE: this is the browser-update step, NOT the trade itself. The order was placed at Broker Accepted At — see Subscriber Lag for the actual per-subscriber trade latency."],
                                    ["Subscriber Lag", "Total per-subscriber latency: parent detected → this subscriber's broker accepted. Submitted At − parent DB SAVED AT."],
                                    ["Reject Reason", "If REJECTED — short error message (insufficient buying power, after-hours, broker_account_missing, etc). Blank for non-rejected orders."],
                                  ] as ([string, string] | [string, string, ChildSortField])[]).map((row) => {
                                    const [h, tip] = row;
                                    const sortField = row[2] as ChildSortField | undefined;
                                    const active = sortField && sort?.field === sortField;
                                    const dir = active ? sort?.dir : undefined;
                                    return (
                                      <th
                                        key={h}
                                        title={tip}
                                        onClick={sortField ? () => cycleSort(sortField) : undefined}
                                        className="text-left px-2 py-2 text-[10px] uppercase tracking-widest font-medium whitespace-nowrap select-none"
                                        style={{
                                          borderBottom: "1px solid var(--border)",
                                          cursor: sortField ? "pointer" : "help",
                                          textDecoration: sortField ? "none" : "underline dotted var(--border)",
                                          textUnderlineOffset: 4,
                                          color: active ? "var(--accent)" : "var(--muted)",
                                        }}
                                      >
                                        <span className="inline-flex items-center gap-1">
                                          <span>{h}</span>
                                          {sortField && (
                                            <SortChevron dir={dir} active={!!active} />
                                          )}
                                        </span>
                                      </th>
                                    );
                                  })}
                                </tr>
                              </thead>
                              <tbody>
                                {applyChildSort(mirrors).map(c => {
                                  const displayName =
                                    c.subscriber_name ||
                                    (c.subscriber_email ? c.subscriber_email.split("@")[0] : null) ||
                                    c.subscriber_user_id.slice(0, 8);
                                  return (
                                    <tr
                                      key={c.order_id}
                                      style={{ borderTop: "1px solid var(--border)", verticalAlign: "top" }}
                                    >
                                      <td className="px-2 py-2 whitespace-nowrap">{displayName}</td>
                                      <td className="px-2 py-2 whitespace-nowrap">
                                        <span
                                          className="inline-block px-2 py-0.5 rounded text-[10px] uppercase tracking-wider font-medium"
                                          style={{
                                            background:
                                              c.status === "rejected"
                                                ? "rgba(239,68,68,0.15)"
                                                : c.status === "filled"
                                                ? "rgba(34,197,94,0.15)"
                                                : c.status === "pending"
                                                ? "rgba(234,179,8,0.15)"
                                                : "rgba(148,163,184,0.15)",
                                            color:
                                              c.status === "rejected"
                                                ? "var(--bad)"
                                                : c.status === "filled"
                                                ? "var(--good)"
                                                : c.status === "pending"
                                                ? "var(--warn)"
                                                : "var(--text-2)",
                                            border: "1px solid",
                                            borderColor:
                                              c.status === "rejected"
                                                ? "rgba(239,68,68,0.3)"
                                                : c.status === "filled"
                                                ? "rgba(34,197,94,0.3)"
                                                : c.status === "pending"
                                                ? "rgba(234,179,8,0.3)"
                                                : "rgba(148,163,184,0.3)",
                                          }}
                                        >
                                          {c.status}
                                        </span>
                                      </td>
                                      <td className="px-2 py-2 tabular-nums whitespace-nowrap">{fmtQty(c.quantity)}</td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: Number(c.filled_quantity) > 0 ? "var(--text)" : "var(--muted)" }}
                                      >
                                        {fmtQty(c.filled_quantity)}
                                      </td>
                                      {/* Mirror's own expected (limit) vs filled price */}
                                      <td className="px-2 py-2 tabular-nums whitespace-nowrap" style={{ color: "var(--muted)" }}>
                                        {c.expected_price ?? "—"}
                                      </td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: c.filled_avg_price ? "var(--text)" : "var(--muted)" }}
                                      >
                                        {c.filled_avg_price ?? "—"}
                                      </td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: "var(--muted)" }}
                                      >
                                        {fmtClock(c.created_at)}
                                      </td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: "var(--muted)" }}
                                      >
                                        {fmtClock(c.subscriber_picked_at)}
                                      </td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: "var(--muted)" }}
                                      >
                                        {fmtClock(c.subscriber_accepted_at)}
                                      </td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: "var(--muted)" }}
                                      >
                                        {fmtClock(c.broker_accepted_at)}
                                      </td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: "var(--muted)" }}
                                      >
                                        {fmtClock(c.redis_published_at)}
                                      </td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: colorFor(c.pick_lag_ms) }}
                                      >
                                        {fmtMs(c.pick_lag_ms)}
                                      </td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: colorFor(c.eligibility_lag_ms) }}
                                      >
                                        {fmtMs(c.eligibility_lag_ms)}
                                      </td>
                                      <td className="px-2 py-2 whitespace-nowrap capitalize" style={{ color: "var(--text-2)" }}>
                                        {c.broker_name ?? "—"}
                                      </td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: colorFor(c.broker_lag_ms) }}
                                      >
                                        {fmtMs(c.broker_lag_ms)}
                                      </td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: colorFor(c.broker_response_ms) }}
                                      >
                                        {fmtMs(c.broker_response_ms)}
                                      </td>
                                      {/* Split bar — Platform (green) vs Broker (blue) */}
                                      {/* <td className="px-2 py-2 whitespace-nowrap">
                                        <PlatformBrokerBar
                                          platformMs={(c.pick_lag_ms ?? 0) + (c.eligibility_lag_ms ?? 0)}
                                          brokerMs={c.broker_lag_ms}
                                        />
                                      </td> */}
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: colorFor(c.publish_lag_ms) }}
                                      >
                                        {fmtMs(c.publish_lag_ms)}
                                      </td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: colorFor(c.subscriber_lag_ms) }}
                                      >
                                        {fmtMs(c.subscriber_lag_ms)}
                                      </td>
                                      <td className="px-2 py-2 whitespace-nowrap">
                                        {c.reject_reason ? (
                                          <span
                                            title={c.reject_reason}
                                            className="text-[11px]"
                                            style={{
                                              color: "var(--bad)",
                                              cursor: "help",
                                              textDecoration: "underline dotted var(--bad)",
                                              textUnderlineOffset: 3,
                                            }}
                                          >
                                            Hover to see error
                                          </span>
                                        ) : (
                                          <span style={{ color: "var(--muted)" }}>—</span>
                                        )}
                                      </td>
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                            </div>
                          )}
    </>
  );
}

/** Shared Performance view — the trader panel renders it against the caller's
 *  own fanouts; the admin per-trader page passes the admin endpoint scoped to
 *  one trader. Same table, cards and drawer either way. */
export function PerformanceView({
  endpoint = "/api/performance/fanouts?limit=50",
}: { endpoint?: string } = {}) {
  const [data, setData] = useState<FanoutResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const reloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function load() {
    try {
      const res = await api<FanoutResponse>(endpoint);
      setData(res);
    } catch {
      // Silent — leave whatever's on screen
    } finally {
      setLoading(false);
    }
  }

  // Initial load + 5s polling.
  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  // SSE: any order.* event triggers a debounced reload so we pick up new
  // fanouts the moment they appear. Debounce so 200 child events from one
  // fanout only trigger one reload.
  useEventStream((evt) => {
    if (!evt.type.startsWith("order.")) return;
    if (reloadTimerRef.current) clearTimeout(reloadTimerRef.current);
    reloadTimerRef.current = setTimeout(load, 600);
  });

  function toggleExpand(id: string) {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  const m = data?.metrics;
  const fanouts = data?.fanouts ?? [];

  // ── Derived data for the charts (memo-free; cheap recompute) ───────
  // Chronological order so the trend chart reads left→right as old→new.
  const fanoutsChrono = [...fanouts].reverse();
  const durationSeries = fanoutsChrono
    .map(f => f.fanout_duration_ms)
    .filter((v): v is number => v !== null && v >= 0);
  const totalSeries = fanoutsChrono
    .map(f => f.total_ms)
    .filter((v): v is number => v !== null && v >= 0);

  // Aggregate subscriber outcomes across all fanouts.
  const subAgg = fanouts.reduce(
    (acc, f) => {
      acc.submitted += f.subscribers.submitted;
      acc.errors += f.subscribers.errors;
      acc.skipped += Math.max(
        0,
        f.subscribers.total - f.subscribers.submitted - f.subscribers.errors,
      );
      return acc;
    },
    { submitted: 0, errors: 0, skipped: 0 },
  );

  // Per-symbol average fanout time (top 6 by count).
  const symbolMap = new Map<string, { sum: number; count: number }>();
  fanouts.forEach(f => {
    if (f.fanout_duration_ms === null) return;
    const e = symbolMap.get(f.symbol) ?? { sum: 0, count: 0 };
    e.sum += f.fanout_duration_ms;
    e.count += 1;
    symbolMap.set(f.symbol, e);
  });
  const symbolRows = [...symbolMap.entries()]
    .map(([symbol, e]) => ({ symbol, avg_ms: Math.round(e.sum / e.count), count: e.count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 6);

  // Centered loader for the initial mount. Auto-refresh after that
  // keeps the table visible while polling silently in the background.
  if (loading && !data) return <PageLoading />;

  return (
    <div className="space-y-5">
      {/* ── Compact metric cards with inline sparklines ───────────────── */}
      {/* <div className="grid grid-cols-2 lg:grid-cols-4 gap-2.5">
        <MetricCard
          label="Fanouts"
          value={String(m?.fanouts_shown ?? 0)}
          sub="last 50 trades"
          Icon={IcoHash}
        />
        <MetricCard
          label="Avg Fanout"
          value={fmtMs(m?.avg_fanout_ms ?? null)}
          valueColor={colorFor(m?.avg_fanout_ms ?? null)}
          Icon={IcoBolt}
        />
        <MetricCard
          label="Max Fanout"
          value={fmtMs(m?.max_fanout_ms ?? null)}
          valueColor={colorFor(m?.max_fanout_ms ?? null)}
          sub="slowest in window"
          Icon={IcoClock}
        />
        <MetricCard
          label="Total Latency"
          value={fmtMs(m?.avg_total_ms ?? null)}
          valueColor={colorFor(m?.avg_total_ms ?? null)}
          Icon={IcoTarget}
        />
      </div> */}

      {/* ── Charts row: trend chart (wide) + donut + symbol bars ───────── */}
      {/* <div className="grid grid-cols-1 lg:grid-cols-12 gap-2.5">
        <div
          className="lg:col-span-7 rounded-lg p-4"
          style={{
            background: "linear-gradient(180deg, rgba(14,20,17,0.7) 0%, rgba(7,9,10,0.4) 100%)",
            border: "1px solid var(--border)",
          }}
        >
          <div className="flex items-center justify-between mb-3">
            <div className="text-[10px] uppercase tracking-widest" style={{ color: "var(--muted)" }}>
              Fanout Latency Trend
            </div>
            <div className="flex items-center gap-3 text-[10px]" style={{ color: "var(--muted)" }}>
              <span className="inline-flex items-center gap-1.5">
                <span style={{ width: 8, height: 2, background: "var(--accent)", display: "inline-block" }} />
                Fanout duration
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span style={{ width: 8, height: 2, background: "var(--good)", display: "inline-block" }} />
                Total
              </span>
            </div>
          </div>
          <div className="relative">
            <LatencyAreaChart values={durationSeries} height={120} color="var(--accent)" />
            <div className="absolute inset-0 pointer-events-none" style={{ mixBlendMode: "screen" }}>
              <LatencyAreaChart values={totalSeries} height={120} color="var(--good)" />
            </div>
          </div>
          <div className="flex justify-between text-[9px] mt-1" style={{ color: "var(--muted)" }}>
            <span>{durationSeries.length > 0 ? "oldest" : ""}</span>
            <span>{durationSeries.length > 0 ? "newest →" : ""}</span>
          </div>
        </div>

        <div
          className="lg:col-span-3 rounded-lg p-4 flex flex-col"
          style={{
            background: "linear-gradient(180deg, rgba(14,20,17,0.7) 0%, rgba(7,9,10,0.4) 100%)",
            border: "1px solid var(--border)",
          }}
        >
          <div className="text-[10px] uppercase tracking-widest mb-3" style={{ color: "var(--muted)" }}>
            Subscriber Outcomes
          </div>
          <div className="flex-1 grid place-items-center">
            <SuccessDonut
              submitted={subAgg.submitted}
              errors={subAgg.errors}
              skipped={subAgg.skipped}
            />
          </div>
        </div>

        <div
          className="lg:col-span-2 rounded-lg p-4 flex flex-col"
          style={{
            background: "linear-gradient(180deg, rgba(14,20,17,0.7) 0%, rgba(7,9,10,0.4) 100%)",
            border: "1px solid var(--border)",
          }}
        >
          <div className="text-[10px] uppercase tracking-widest mb-3" style={{ color: "var(--muted)" }}>
            Top Symbols
          </div>
          <div className="flex-1">
            <SymbolBars rows={symbolRows} />
          </div>
        </div>
      </div> */}

      {/* ── Table ──────────────────────────────────────────────────────── */}
      {/* overflow-auto + a viewport-relative max-h enables BOTH horizontal
          scroll (wide table) and vertical scroll. The max-h subtracts a
          rough allowance for the AppShell topbar + page header above so the
          table fills the rest of the screen instead of having an arbitrary
          70vh cap. The sticky thead below stays pinned to the top of this
          scroll container so column headers remain visible while scrolling. */}
      <div
        className="overflow-auto rounded-xl"
        style={{
          border: "1px solid var(--border)",
          background: "var(--panel)",
          maxHeight: "calc(100vh - 120px)",
          // When there are no rows, give the container a definite height so
          // the empty state can sit in the vertical middle of the table area
          // rather than bunched under the header.
          ...(!loading && fanouts.length === 0 ? { height: "calc(100vh - 120px)" } : {}),
        }}
      >
        <table
          className={`w-full text-sm ${!loading && fanouts.length === 0 ? "h-full" : ""}`}
          style={{ borderCollapse: "separate", borderSpacing: 0 }}
        >
          {/* z-10 keeps the header above scrolling cells; the opaque panel
              background prevents row text from bleeding through behind the
              sticky header (which would otherwise be transparent). */}
          <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
            <tr style={{ color: "var(--muted)" }}>
              {([
                ["Symbol", "Ticker symbol the trader bought or sold."],
                ["Side", "BUY or SELL."],
                ["Qty", "Trader's own order quantity. Each subscriber's mirror is this × their multiplier."],
                ["Expected Price", "The trader's limit price. Blank for market orders (no expected price)."],
                ["Filled Price", "The broker's average fill price for this order. Compare with Expected Price to gauge slippage."],
                ["Date", "Calendar date of the trade (US Eastern). The timestamp columns show time-of-day only."],
                ["Trader Submitted At", "When our backend received the trader's order. For trades placed outside our app (Alpaca dashboard, mobile, broker API), this is the time Alpaca accepted the order."],
                ["Broker Accepted At", "When the trader's broker (Alpaca) confirmed acceptance of the order."],
                ["Trader Listened At", "When our Alpaca trade-updates WebSocket heard the order event from the broker."],
                ["DB SAVED AT", "When we created the parent Order row in our database — this is the trigger that starts fanout to subscribers."],
                ["PUBLISHED FOR SUBS AT", "When we broadcast the order via SSE so the trader's open browser tabs update in real time."],
                ["ALL SUBS COMPLETED AT", "The latest moment any subscriber's broker accepted their mirror — i.e. max(Submitted At) across all child orders. The 'last subscriber filled' time."],
                ["API→Broker Lag", "Trader submit → broker accept. Broker Accepted At − Trader Submitted At."],
                ["UI Notification Lag", "Detection → SSE broadcast to the trader's browser. PUBLISHED FOR SUBS AT − DB SAVED AT. NOTE: this is the browser-update step, NOT the trade itself. The trade was placed at Broker Accepted At."],
                ["Detection Lag", "Broker accept → our DB row created. DB SAVED AT − Broker Accepted At. Near-zero for orders placed through our Trade Panel; larger for externally-placed trades detected via WebSocket."],
                ["Platform Lag", "End-to-end time spent fanning out to every subscriber. ALL SUBS COMPLETED AT − DB SAVED AT."],
                ["Total", "Client-facing latency: trader submit → last subscriber's broker accepted. ALL SUBS COMPLETED AT − Broker Accepted At."],
                ["Lowest Broker Lag", "Fastest subscriber: minimum broker_lag (mirror-submit → broker-accept) across all subscriber children for this trade."],
                ["Average Broker Lag", "Mean broker_lag across all subscriber children for this trade."],
                ["Highest Broker Lag", "Slowest subscriber: maximum broker_lag across all subscriber children for this trade."],
                ["Subscribers", "Total subscribers receiving this trade, with submitted-vs-error counts."],
              ] as [string, string][]).map(([h, tip]) => (
                <th
                  key={h}
                  title={tip}
                  className="text-left px-2 md:px-3 py-2 md:py-3 text-[10px] uppercase tracking-widest font-medium whitespace-nowrap"
                  style={{
                    borderBottom: "1px solid var(--border)",
                    // Dotted underline + help cursor signals "hover me for an
                    // explanation" without bloating the header with ? icons.
                    cursor: "help",
                    textDecoration: "underline dotted var(--border)",
                    textUnderlineOffset: 4,
                  }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading && fanouts.length === 0 && (
              <tr>
                <td colSpan={21} className="px-3 py-10 text-center" style={{ color: "var(--muted)" }}>
                  <span className="inline-flex items-center gap-2">
                    <Spinner />
                    <span>Loading fanouts…</span>
                  </span>
                </td>
              </tr>
            )}
            {!loading && fanouts.length === 0 && (
              <tr>
                <td colSpan={21} className="px-3 align-middle text-center" style={{ color: "var(--muted)" }}>
                  <div className="flex items-center justify-center min-h-[240px]">
                    No fanouts yet. Place a trade to see latency metrics here.
                  </div>
                </td>
              </tr>
            )}
            {fanouts.map(f => {
              const isOpen = expanded.has(f.parent_order_id);
              const blStats = brokerLagStats(f.children);
              return (
                <Fragment key={f.parent_order_id}>
                  <tr
                    onClick={() => toggleExpand(f.parent_order_id)}
                    className="cursor-pointer transition-colors hover:bg-[var(--panel-2)]"
                    style={{ borderTop: "1px solid var(--border)" }}
                  >
                    <td className="px-2 md:px-3 py-2 md:py-3 font-medium whitespace-nowrap">
                      <span className="inline-flex items-center gap-2">
                        <span
                          aria-hidden
                          style={{
                            display: "inline-block",
                            width: 10,
                            color: "var(--muted)",
                            transform: isOpen ? "rotate(90deg)" : "rotate(0deg)",
                            transition: "transform 150ms",
                          }}
                        >
                          ▸
                        </span>
                        {f.symbol}
                      </span>
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3">
                      <span style={{ color: f.side === "buy" ? "var(--good)" : "var(--bad)", fontWeight: 600 }}>
                        {f.side.toUpperCase()}
                      </span>
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums">{fmtQty(f.quantity)}</td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums">{fmtPrice(f.expected_price)}</td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums">{fmtPrice(f.filled_avg_price)}</td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums whitespace-nowrap" style={{ color: "var(--muted)" }}>
                      {fmtDate(f.broker_accepted_at ?? f.detected_at)}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {fmtClock(f.trader_submitted_at)}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {fmtClock(f.broker_accepted_at)}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {fmtClock(f.socket_received_at)}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {fmtClock(f.detected_at)}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {fmtClock(f.redis_published_at)}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {fmtClock(f.fanout_completed_at)}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums" style={{ color: colorFor(f.api_to_broker_lag_ms) }}>
                      {fmtMs(f.api_to_broker_lag_ms)}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums" style={{ color: colorFor(f.publish_lag_ms) }}>
                      {fmtMs(f.publish_lag_ms)}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums" style={{ color: colorFor(f.detection_lag_ms) }}>
                      {fmtMs(f.detection_lag_ms)}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums" style={{ color: colorFor(f.fanout_duration_ms) }}>
                      {fmtMs(f.fanout_duration_ms)}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums" style={{ color: colorFor(f.total_ms) }}>
                      {fmtMs(f.total_ms)}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums whitespace-nowrap" style={{ color: colorFor(blStats.min) }}>
                      {fmtMs(blStats.min)}
                      {blStats.minBroker && (
                        <span className="ml-1.5 text-[10px]" style={{ color: "var(--muted)" }}>
                          ({blStats.minBroker})
                        </span>
                      )}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums whitespace-nowrap" style={{ color: colorFor(blStats.avg) }}>
                      {fmtMs(blStats.avg)}
                      {blStats.avgBroker && (
                        <span className="ml-1.5 text-[10px]" style={{ color: "var(--muted)" }}>
                          ({blStats.avgBroker})
                        </span>
                      )}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3 tabular-nums whitespace-nowrap" style={{ color: colorFor(blStats.max) }}>
                      {fmtMs(blStats.max)}
                      {blStats.maxBroker && (
                        <span className="ml-1.5 text-[10px]" style={{ color: "var(--muted)" }}>
                          ({blStats.maxBroker})
                        </span>
                      )}
                    </td>
                    <td className="px-2 md:px-3 py-2 md:py-3">
                      <SubscriberPill counts={f.subscribers} />
                    </td>
                  </tr>

                  {/* ── Per-subscriber expansion ──────────────────────── */}
                  {isOpen && (
                    <tr style={{ borderTop: "1px solid var(--border)" }}>
                      <td colSpan={21} className="px-0 py-0" style={{ background: "var(--panel-2)" }}>
                        <div className="px-5 py-4">
                          <SubscriberBreakdown mirrors={f.children} />
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* ── Footnote (matches the screenshot terminology) ────────────────
          Hidden for now per UX request — the column-header tooltips on the
          table itself cover the same content on hover, so the long inline
          legend was visual noise. Restore by removing this comment block
          if we ever want the legend back as a permanent footer.
      <div className="text-xs leading-relaxed space-y-2" style={{ color: "var(--muted)" }}>
        <div className="flex flex-wrap gap-x-5 gap-y-1 pb-1" style={{ borderBottom: "1px solid var(--border)" }}>
          <span className="inline-flex items-center gap-1.5">
            <span style={{ width: 8, height: 8, borderRadius: 2, background: "var(--good)", display: "inline-block" }} />
            <strong style={{ color: "var(--good)" }}>Platform Lag</strong>
            <span>— time from trader&apos;s order detected → copy orders submitted. What the platform controls (detection + queue + eligibility checks).</span>
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span style={{ width: 8, height: 8, borderRadius: 2, background: "#3b82f6", display: "inline-block" }} />
            <strong style={{ color: "#3b82f6" }}>Broker Lag</strong>
            <span>— time the broker (Alpaca) takes to confirm each copy order after we submit it. External — varies with broker server load, not platform speed.</span>
          </span>
        </div>
        <p>
          <strong style={{ color: "var(--text-2)" }}>Detection lag</strong> = time between Alpaca accepting your order and
          our backend creating the parent Order row (≈0ms for orders placed via our API; meaningful only for
          orders detected via the Alpaca trade_updates WebSocket).{" "}
          <strong style={{ color: "var(--text-2)" }}>Fanout duration</strong> = time from our detection to the last
          subscriber&apos;s order being accepted at their broker (parallel via asyncio.gather + per-broker semaphore).{" "}
          <strong style={{ color: "var(--text-2)" }}>Total</strong> = end-to-end (Alpaca-accept → last subscriber
          submitted). <strong style={{ color: "var(--text-2)" }}>Subscriber lag</strong> (per row when expanded) = our
          detection → that subscriber&apos;s broker accept.
        </p>
        <p>
          New per-step lifecycle stamps (alembic <code>e7a1d2c40f01</code>):{" "}
          <strong style={{ color: "var(--text-2)" }}>Trader Submitted At</strong> = our backend received the trader&apos;s
          submit (or Alpaca&apos;s receive time for externally-placed orders).{" "}
          <strong style={{ color: "var(--text-2)" }}>Trader Listened At</strong> = our Alpaca trade_updates listener
          heard the event (NULL for in-app orders).{" "}
          <strong style={{ color: "var(--text-2)" }}>PUBLISHED FOR SUBS AT</strong> = SSE event broadcast to subscribers.{" "}
          <strong style={{ color: "var(--text-2)" }}>Picked At / Accepted At / Broker Accepted At</strong> (per-child) =
          when copy_engine picked the subscriber, passed eligibility, and their broker accepted, respectively.
        </p>
      </div>
      */}
    </div>
  );
}
