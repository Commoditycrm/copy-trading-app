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
import { api } from "@/lib/api";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";

interface SubscriberCounts { total: number; submitted: number; errors: number; }
interface FanoutChild {
  order_id: string;
  subscriber_user_id: string;
  subscriber_email: string | null;
  subscriber_name: string | null;
  status: string;
  quantity: string;
  filled_quantity: string;
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
  publish_lag_ms: number | null;
}
interface FanoutRow {
  parent_order_id: string;
  symbol: string;
  side: string;
  quantity: string;
  instrument_type: string;
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
  return `${(ms / 1000).toFixed(2)}s`;
}

/** HH:MM:SS.mmm in the user's local timezone — matches the screenshot. */
function fmtClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  const ms = String(d.getMilliseconds()).padStart(3, "0");
  return `${hh}:${mm}:${ss}.${ms}`;
}

// ── Compact metric card with optional inline sparkline ────────────────

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

function SubscriberPill({ counts }: { counts: SubscriberCounts }) {
  // "6 ✓ / 0 ✗ of 6" — green ok, red errors, neutral denominator.
  return (
    <span className="inline-flex items-center gap-1 text-xs">
      <span style={{ color: "var(--good)" }}>{counts.submitted} ✓</span>
      <span style={{ color: "var(--muted)" }}>/</span>
      <span style={{ color: counts.errors > 0 ? "var(--bad)" : "var(--muted)" }}>
        {counts.errors} ✗
      </span>
      <span style={{ color: "var(--muted)" }}>of {counts.total}</span>
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
      <div className="mt-2 text-[11px]" style={{ color: "var(--muted)" }}>
        Note: per-subscriber timings below show <b>trade latency</b> (Subscriber Lag). The
        separate <b>UI Notification Lag</b> column is when the subscriber&apos;s browser
        received the SSE update — independent of when their order was actually placed.
      </div>
    </div>
  );
}

export default function PerformancePage() {
  const [data, setData] = useState<FanoutResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const reloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function load() {
    try {
      const res = await api<FanoutResponse>("/api/performance/fanouts?limit=50");
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

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-2xl" style={{ fontWeight: 600 }}>Fanout Performance</h1>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          Latency breakdown for your most recent trades that fanned out to subscribers.
          Click any row to see per-subscriber timing. Auto-refreshes every 5 seconds and
          on every new trade event.
        </p>
      </header>

      {/* ── Compact metric cards with inline sparklines ───────────────── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-2.5">
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
      </div>

      {/* ── Charts row: trend chart (wide) + donut + symbol bars ───────── */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-2.5">
        {/* Latency trend */}
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
            {/* Overlay the total series in a different color, same scale */}
            <div className="absolute inset-0 pointer-events-none" style={{ mixBlendMode: "screen" }}>
              <LatencyAreaChart values={totalSeries} height={120} color="var(--good)" />
            </div>
          </div>
          <div className="flex justify-between text-[9px] mt-1" style={{ color: "var(--muted)" }}>
            <span>{durationSeries.length > 0 ? "oldest" : ""}</span>
            <span>{durationSeries.length > 0 ? "newest →" : ""}</span>
          </div>
        </div>

        {/* Success donut */}
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

        {/* Per-symbol bars */}
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
      </div>

      {/* ── Table ──────────────────────────────────────────────────────── */}
      <div
        className="overflow-x-auto rounded-xl"
        style={{
          border: "1px solid var(--border)",
          background: "linear-gradient(180deg, rgba(14,20,17,0.5) 0%, rgba(7,9,10,0.3) 100%)",
        }}
      >
        <table className="w-full text-sm" style={{ borderCollapse: "separate", borderSpacing: 0 }}>
          <thead>
            <tr style={{ color: "var(--muted)" }}>
              {([
                ["Symbol", "Ticker symbol the trader bought or sold."],
                ["Side", "BUY or SELL."],
                ["Qty", "Trader's own order quantity. Each subscriber's mirror is this × their multiplier."],
                ["Trader Submitted At", "When our backend received the trader's order. For trades placed outside our app (Alpaca dashboard, mobile, broker API), this is the time Alpaca accepted the order."],
                ["Broker Accepted At", "When the trader's broker (Alpaca) confirmed acceptance of the order."],
                ["Socket Received At", "When our Alpaca trade-updates WebSocket heard the order event from the broker."],
                ["Detected At", "When we created the parent Order row in our database — this is the trigger that starts fanout to subscribers."],
                ["Redis Published At", "When we broadcast the order via SSE so the trader's open browser tabs update in real time."],
                ["Fanout Completed At", "The latest moment any subscriber's broker accepted their mirror — i.e. max(Submitted At) across all child orders. The 'last subscriber filled' time."],
                ["API→Broker Lag", "Trader submit → broker accept. Broker Accepted At − Trader Submitted At."],
                ["Socket Lag", "Trader submit → our WebSocket hearing about it. Socket Received At − Trader Submitted At."],
                ["UI Notification Lag", "Detection → SSE broadcast to the trader's browser. Redis Published At − Detected At. NOTE: this is the browser-update step, NOT the trade itself. The trade was placed at Broker Accepted At."],
                ["Detection Lag", "Broker accept → our DB row created. Detected At − Broker Accepted At. Near-zero for orders placed through our Trade Panel; larger for externally-placed trades detected via WebSocket."],
                ["Fanout Duration", "End-to-end time spent fanning out to every subscriber. Fanout Completed At − Detected At."],
                ["Total", "Client-facing latency: trader submit → last subscriber's broker accepted. Fanout Completed At − Broker Accepted At."],
                ["Subscribers", "Total subscribers receiving this trade, with submitted-vs-error counts."],
              ] as [string, string][]).map(([h, tip]) => (
                <th
                  key={h}
                  title={tip}
                  className="text-left px-3 py-3 text-[10px] uppercase tracking-widest font-medium whitespace-nowrap"
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
                <td colSpan={16} className="px-3 py-10 text-center" style={{ color: "var(--muted)" }}>
                  <span className="inline-flex items-center gap-2">
                    <Spinner />
                    <span>Loading fanouts…</span>
                  </span>
                </td>
              </tr>
            )}
            {!loading && fanouts.length === 0 && (
              <tr>
                <td colSpan={16} className="px-3 py-10 text-center" style={{ color: "var(--muted)" }}>
                  No fanouts yet. Place a trade to see latency metrics here.
                </td>
              </tr>
            )}
            {fanouts.map(f => {
              const isOpen = expanded.has(f.parent_order_id);
              return (
                <Fragment key={f.parent_order_id}>
                  <tr
                    onClick={() => toggleExpand(f.parent_order_id)}
                    className="cursor-pointer transition-colors hover:bg-white/5"
                    style={{ borderTop: "1px solid var(--border)" }}
                  >
                    <td className="px-3 py-3 font-medium whitespace-nowrap">
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
                    <td className="px-3 py-3">
                      <span style={{ color: f.side === "buy" ? "var(--good)" : "var(--bad)", fontWeight: 600 }}>
                        {f.side.toUpperCase()}
                      </span>
                    </td>
                    <td className="px-3 py-3 tabular-nums">{f.quantity}</td>
                    <td className="px-3 py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {fmtClock(f.trader_submitted_at)}
                    </td>
                    <td className="px-3 py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {fmtClock(f.broker_accepted_at)}
                    </td>
                    <td className="px-3 py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {fmtClock(f.socket_received_at)}
                    </td>
                    <td className="px-3 py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {fmtClock(f.detected_at)}
                    </td>
                    <td className="px-3 py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {fmtClock(f.redis_published_at)}
                    </td>
                    <td className="px-3 py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {fmtClock(f.fanout_completed_at)}
                    </td>
                    <td className="px-3 py-3 tabular-nums" style={{ color: colorFor(f.api_to_broker_lag_ms) }}>
                      {fmtMs(f.api_to_broker_lag_ms)}
                    </td>
                    <td className="px-3 py-3 tabular-nums" style={{ color: colorFor(f.socket_lag_ms) }}>
                      {fmtMs(f.socket_lag_ms)}
                    </td>
                    <td className="px-3 py-3 tabular-nums" style={{ color: colorFor(f.publish_lag_ms) }}>
                      {fmtMs(f.publish_lag_ms)}
                    </td>
                    <td className="px-3 py-3 tabular-nums" style={{ color: colorFor(f.detection_lag_ms) }}>
                      {fmtMs(f.detection_lag_ms)}
                    </td>
                    <td className="px-3 py-3 tabular-nums" style={{ color: colorFor(f.fanout_duration_ms) }}>
                      {fmtMs(f.fanout_duration_ms)}
                    </td>
                    <td className="px-3 py-3 tabular-nums" style={{ color: colorFor(f.total_ms) }}>
                      {fmtMs(f.total_ms)}
                    </td>
                    <td className="px-3 py-3">
                      <SubscriberPill counts={f.subscribers} />
                    </td>
                  </tr>

                  {/* ── Per-subscriber expansion ──────────────────────── */}
                  {isOpen && (
                    <tr style={{ borderTop: "1px solid var(--border)" }}>
                      <td colSpan={16} className="px-0 py-0" style={{ background: "rgba(0,0,0,0.25)" }}>
                        <div className="px-5 py-4">
                          {/* Headline summary — the client-friendly framing.
                              Avoids the "trade took 15.9s" misread by showing
                              what most subscribers actually experienced (p50,
                              # placed within 1s) and naming the slowest as a
                              named outlier rather than a platform stat. */}
                          {f.children.length > 0 && <TradeSummaryCard mirrors={f.children} />}
                          <div
                            className="text-[10px] uppercase tracking-widest mb-3"
                            style={{ color: "var(--muted)" }}
                          >
                            Per-Subscriber Timeline ({f.children.length} target{f.children.length === 1 ? "" : "s"})
                          </div>
                          {f.children.length === 0 ? (
                            <div className="text-xs" style={{ color: "var(--muted)" }}>
                              No subscribers received this trade.
                            </div>
                          ) : (
                            <table
                              className="w-full text-xs"
                              style={{ borderCollapse: "separate", borderSpacing: 0, tableLayout: "auto" }}
                            >
                              <thead>
                                <tr style={{ color: "var(--muted)" }}>
                                  {([
                                    ["Subscriber", "The subscriber whose account this mirror was placed on."],
                                    ["Status", "Current state of this mirror order (PENDING / SUBMITTED / FILLED / REJECTED / RETRY_PENDING / etc)."],
                                    ["Qty", "Mirror quantity — trader's qty × this subscriber's multiplier, rounded per broker rules (floored to whole shares unless the broker supports fractional)."],
                                    ["Filled Qty", "Quantity actually filled by the subscriber's broker. Less than Qty means a partial fill."],
                                    ["Created At", "When we inserted this subscriber's child Order row in our database (status=PENDING)."],
                                    ["Picked At", "When copy_engine started processing this specific subscriber — the per-subscriber starting line."],
                                    ["Accepted At", "When this subscriber passed every eligibility check (daily-loss limit not hit, copy still enabled, broker available, scaled qty > 0). We're about to call their broker."],
                                    ["Broker Accepted At", "When this subscriber's broker (Alpaca) confirmed acceptance of the mirror order."],
                                    ["Published At", "When we broadcast the mirror's outcome via SSE so the subscriber's open tabs update in real time."],
                                    ["Submitted At", "Broker's own timestamp for when it accepted the order. Usually identical to Broker Accepted At; can differ if the broker's clock is skewed."],
                                    ["Pick Lag", "Parent detected → this subscriber picked. Picked At − parent Detected At. Grows with the number of subscribers ahead of this one in the fanout queue."],
                                    ["Eligibility Lag", "Picked → ready to call broker. Accepted At − Picked At. Time spent on gate checks (daily-loss P&L lookup, settings reads)."],
                                    ["Broker Lag", "Submit → broker accepted. Broker Accepted At − Accepted At. The single broker REST call's round-trip."],
                                    ["UI Notification Lag", "Broker accept → SSE pushed to subscriber's browser. Published At − Broker Accepted At. NOTE: this is the browser-update step, NOT the trade itself. The order was placed at Broker Accepted At — see Subscriber Lag for the actual per-subscriber trade latency."],
                                    ["Subscriber Lag", "Total per-subscriber latency: parent detected → this subscriber's broker accepted. Submitted At − parent Detected At."],
                                    ["Broker Order ID", "Identifier the subscriber's broker assigned to this mirror. Used by support to look up the order on the broker side."],
                                    ["Reject Reason", "If REJECTED — short error message (insufficient buying power, after-hours, broker_account_missing, etc). Blank for non-rejected orders."],
                                  ] as [string, string][]).map(([h, tip]) => (
                                    <th
                                      key={h}
                                      title={tip}
                                      className="text-left px-2 py-2 text-[10px] uppercase tracking-widest font-medium whitespace-nowrap"
                                      style={{
                                        borderBottom: "1px solid var(--border)",
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
                                {f.children.map(c => {
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
                                      <td className="px-2 py-2 tabular-nums whitespace-nowrap">{c.quantity}</td>
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: Number(c.filled_quantity) > 0 ? "var(--text)" : "var(--muted)" }}
                                      >
                                        {c.filled_quantity}
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
                                        style={{ color: "var(--muted)" }}
                                      >
                                        {fmtClock(c.submitted_at)}
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
                                      <td
                                        className="px-2 py-2 tabular-nums whitespace-nowrap"
                                        style={{ color: colorFor(c.broker_lag_ms) }}
                                      >
                                        {fmtMs(c.broker_lag_ms)}
                                      </td>
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
                                      <td
                                        className="px-2 py-2 font-mono text-[10px] whitespace-nowrap"
                                        style={{ color: "var(--muted)" }}
                                        title={c.broker_order_id ?? undefined}
                                      >
                                        {c.broker_order_id
                                          ? c.broker_order_id.length > 18
                                            ? c.broker_order_id.slice(0, 18) + "…"
                                            : c.broker_order_id
                                          : "—"}
                                      </td>
                                      <td
                                        className="px-2 py-2"
                                        style={{
                                          color: "var(--bad)",
                                          // Long JSON / error strings wrap; break on any char so
                                          // a raw response body doesn't blow the column width.
                                          wordBreak: "break-word",
                                          whiteSpace: "normal",
                                          minWidth: 240,
                                          maxWidth: 480,
                                          lineHeight: 1.4,
                                        }}
                                      >
                                        {c.reject_reason || ""}
                                      </td>
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          )}
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

      {/* ── Footnote (matches the screenshot terminology) ──────────────── */}
      <div className="text-xs leading-relaxed space-y-2" style={{ color: "var(--muted)" }}>
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
          <strong style={{ color: "var(--text-2)" }}>Socket Received At</strong> = our Alpaca trade_updates listener
          heard the event (NULL for in-app orders).{" "}
          <strong style={{ color: "var(--text-2)" }}>Redis Published At</strong> = SSE event broadcast to subscribers.{" "}
          <strong style={{ color: "var(--text-2)" }}>Picked At / Accepted At / Broker Accepted At</strong> (per-child) =
          when copy_engine picked the subscriber, passed eligibility, and their broker accepted, respectively.
        </p>
      </div>
    </div>
  );
}
