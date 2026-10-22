"use client";

/**
 * Admin System → "AWS Testing — Cost & Usage". QA (EC2) only; production on
 * Lightsail is shown as excluded, never added in. The figures are a snapshot a
 * host timer takes from Cost Explorer every 8h, so reloading here is free and
 * this never rides the 3s CPU poll. Unknown figures render as "Unavailable",
 * never as $0.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

interface ServiceRow {
  category: string;
  service: string;
  usage_type: string;
  resource: string;
  usage: string;
  direct_usd: string;
  allocated_usd: string;
  cost_usd: string;
  billing_type: string;
}

interface CostPayload {
  status: "ok" | "no_attribution" | "unavailable";
  reason?: string;
  error_code?: string;
  action_needed?: string;
  required_permissions?: string[];
  currency?: string;
  metric?: string;
  region?: string;
  instance_id?: string | null;
  attribution?: {
    direct_source: string | null;
    allocated_source: string | null;
    allocation_verified: { verified: boolean; reason?: string | null; instances_in_region?: number } | null;
  };
  month_to_date?: {
    qa_total_usd: string | null;
    direct_usd: string | null;
    allocated_usd: string | null;
    unattributed_usd: string | null;
    production_lightsail_excluded_usd: string | null;
    account_total_usd: string | null;
    account_credits_usd?: string | null;
    account_net_usd?: string | null;
    estimated: boolean;
  };
  previous_month?: { month: string; qa_total_usd: string | null; account_total_usd: string | null };
  basis?: string;
  projected_month_usd?: string | null;
  projection_method?: string | null;
  average_daily_usd?: string | null;
  services?: ServiceRow[];
  daily?: { date: string; cost_usd: string; estimated: boolean }[];
  data_through?: string | null;
  unattributed_by_service?: { service: string; cost_usd: string; production: boolean }[];
  fetched_at?: string;
  interval_seconds?: number;
  age_seconds?: number | null;
  stale?: boolean;
}

const POLL_MS = 30 * 60 * 1000;
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const fmt = (v: string | null | undefined) => (v == null ? "Unavailable" : usd.format(Number(v)));
const card = "rounded-xl p-4";
const cardStyle = { background: "var(--panel-2)", border: "1px solid var(--border)" };
const muted = { color: "var(--muted)" };
const warn = "#b45309";

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  const unavailable = value === "Unavailable";
  return (
    <div className={card} style={cardStyle}>
      <div className="text-xs" style={muted}>{label}</div>
      <div className="text-lg font-semibold mt-1 tabular-nums" style={unavailable ? muted : undefined}>{value}</div>
      {sub && <div className="text-xs mt-1" style={muted}>{sub}</div>}
    </div>
  );
}

/** One series → no legend; the heading names it. Bars round only at the top,
 *  2px gaps, a recessive baseline, hover tooltip, and a table view. */
function DailyTrend({ daily }: { daily: NonNullable<CostPayload["daily"]> }) {
  const [hover, setHover] = useState<number | null>(null);
  const [asTable, setAsTable] = useState(false);
  const vals = daily.map((d) => Number(d.cost_usd));
  const max = Math.max(...vals, 0.01);
  const W = 640, H = 140, pad = 4, gap = 2;
  const bw = Math.max(2, (W - gap * (daily.length - 1)) / Math.max(daily.length, 1));
  return (
    <div className={card} style={cardStyle}>
      <div className="flex items-baseline justify-between mb-2">
        <div className="text-sm font-semibold" style={{ color: "var(--text-2)" }}>Daily QA cost (this month)</div>
        <button className="text-xs underline" style={muted} onClick={() => setAsTable((v) => !v)}>
          {asTable ? "Show chart" : "Show table"}
        </button>
      </div>
      {daily.length === 0 ? (
        <div className="text-xs" style={muted}>No daily data yet.</div>
      ) : asTable ? (
        <table className="w-full text-xs tabular-nums">
          <thead><tr style={muted}><th className="text-left py-1">Date</th><th className="text-right">Cost</th><th className="text-right">Status</th></tr></thead>
          <tbody>
            {daily.map((d) => (
              <tr key={d.date} style={{ borderTop: "1px solid var(--border)" }}>
                <td className="py-1">{d.date}</td>
                <td className="text-right">{usd.format(Number(d.cost_usd))}</td>
                <td className="text-right" style={muted}>{d.estimated ? "estimated" : "final"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="relative">
          <svg viewBox={`0 0 ${W} ${H + 16}`} className="w-full" role="img"
               aria-label={`Daily QA cost, ${daily[0].date} to ${daily[daily.length - 1].date}`}
               onMouseLeave={() => setHover(null)}>
            <line x1={0} x2={W} y1={H} y2={H} stroke="var(--border)" strokeWidth={1} />
            <text x={0} y={10} fontSize={10} fill="var(--muted)">{usd.format(max)}</text>
            {daily.map((d, i) => {
              const h = Math.max(1, ((vals[i] / max) * (H - 16)));
              const x = i * (bw + gap);
              const y = H - h;
              const r = Math.min(4, bw / 2, h);
              const path = `M${x},${H} V${y + r} Q${x},${y} ${x + r},${y} H${x + bw - r} Q${x + bw},${y} ${x + bw},${y + r} V${H} Z`;
              return (
                <g key={d.date} onMouseEnter={() => setHover(i)}>
                  {/* hit target taller than the bar */}
                  <rect x={x} y={0} width={bw + gap} height={H} fill="transparent" />
                  <path d={path} fill="var(--accent)" opacity={hover == null || hover === i ? (d.estimated ? 0.55 : 1) : 0.35} />
                </g>
              );
            })}
            <text x={0} y={H + 13} fontSize={10} fill="var(--muted)">{daily[0].date.slice(5)}</text>
            <text x={W} y={H + 13} fontSize={10} fill="var(--muted)" textAnchor="end">{daily[daily.length - 1].date.slice(5)}</text>
          </svg>
          {hover != null && (
            <div className="absolute pointer-events-none text-xs rounded-md px-2 py-1 tabular-nums"
                 style={{ left: `${((hover * (bw + gap)) / W) * 100}%`, top: 0, transform: "translate(-30%, -110%)",
                          background: "var(--panel)", border: "1px solid var(--border)", color: "var(--text-2)" }}>
              {daily[hover].date} · {usd.format(vals[hover])}{daily[hover].estimated ? " (estimated)" : ""}
            </div>
          )}
          <div className="text-[11px] mt-1" style={muted}>Paler bars are AWS estimates that may still change.</div>
        </div>
      )}
    </div>
  );
}

export default function AwsCostSection() {
  const [c, setC] = useState<CostPayload | null>(null);
  const [err, setErr] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (manual = false) => {
    setBusy(true);
    try {
      const r = await api<CostPayload>("/api/admin/system/aws-costs");
      setC(r);
      setErr(false);
    } catch (e) {
      setErr(true);
      if (manual) notify.fromError(e, "Could not load AWS costs");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(() => load(), POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  const m = c?.month_to_date;
  const fetched = c?.fetched_at ? new Date(c.fetched_at) : null;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-bold">AWS Testing — Cost &amp; Usage</h3>
          <p className="text-xs mt-0.5" style={muted}>
            QA environment on EC2{c?.instance_id ? ` (${c.instance_id}, ${c.region})` : ""} only. Production on Lightsail is excluded.
            {c?.metric ? ` ${c.metric}, ${c.currency}${c.basis ? `, ${c.basis}` : ""}.` : ""}
          </p>
        </div>
        <button onClick={() => load(true)} disabled={busy}
                className="text-sm px-3 py-1.5 rounded-lg"
                style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)", opacity: busy ? 0.6 : 1 }}>
          {busy ? "Loading…" : "Reload"}
        </button>
      </div>

      {c?.stale && c.status !== "unavailable" && (
        <div className="rounded-xl p-3 text-xs" style={{ ...cardStyle, border: `1px solid ${warn}`, color: warn }}>
          These figures are out of date{c.age_seconds != null ? ` (collected ${Math.round(c.age_seconds / 3600)}h ago)` : ""} —
          the cost collector on the QA host has missed its runs.
        </div>
      )}

      {err && !c && (
        <div className={card} style={{ ...cardStyle, border: "1px solid var(--bad, #b3261e)", color: "var(--bad, #b3261e)" }}>
          <span className="text-sm">Couldn&apos;t reach the AWS cost endpoint.</span>
        </div>
      )}

      {c?.status === "unavailable" && (
        <div className={card} style={{ ...cardStyle, border: `1px solid ${warn}` }}>
          <div className="text-sm font-semibold" style={{ color: warn }}>AWS cost data unavailable</div>
          <div className="text-xs mt-1" style={{ color: "var(--text-2)" }}>{c.reason}</div>
          {c.action_needed && <div className="text-xs mt-1" style={muted}>What&apos;s needed: {c.action_needed}</div>}
          {c.required_permissions && c.error_code !== "disabled" && (
            <div className="text-xs mt-1" style={muted}>Read-only permissions: {c.required_permissions.join(", ")}</div>
          )}
        </div>
      )}

      {c && c.status !== "unavailable" && m && (
        <>
          {c.status === "no_attribution" && (
            <div className="rounded-xl p-3 text-xs" style={{ ...cardStyle, border: `1px solid ${warn}`, color: warn }}>
              No QA attribution source is configured (cost-allocation tag or regional EC2 allocation), so no figure is
              shown as QA cost. Account spend below is unattributed.
            </div>
          )}

          <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))" }}>
            <Stat label="Month-to-date (QA)" value={fmt(m.qa_total_usd)}
                  sub={m.estimated ? "includes AWS estimates" : undefined} />
            <Stat label="Projected this month" value={fmt(c.projected_month_usd)}
                  sub={c.projection_method === "aws_forecast" ? "AWS forecast" : c.projection_method ? "linear from daily average" : undefined} />
            <Stat label={`Previous month${c.previous_month ? ` (${c.previous_month.month})` : ""}`} value={fmt(c.previous_month?.qa_total_usd)} />
            <Stat label="Average daily" value={fmt(c.average_daily_usd)} sub="complete days only" />
          </div>

          <div className={card} style={cardStyle}>
            <div className="text-sm font-semibold mb-2" style={{ color: "var(--text-2)" }}>Services (month-to-date)</div>
            {(c.services ?? []).length === 0 ? (
              <div className="text-xs" style={muted}>No QA charges attributed this month.</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs tabular-nums">
                  <thead>
                    <tr style={muted}>
                      <th className="text-left py-1 pr-2">Service</th><th className="text-left pr-2">Resource</th>
                      <th className="text-left pr-2">Usage</th><th className="text-right pr-2">Cost</th>
                      <th className="text-left pr-2">Attribution</th><th className="text-left">Billing type</th>
                    </tr>
                  </thead>
                  <tbody>
                    {c.services!.map((s) => {
                      const direct = Number(s.direct_usd), alloc = Number(s.allocated_usd);
                      return (
                        <tr key={`${s.service}|${s.usage_type}`} style={{ borderTop: "1px solid var(--border)" }}>
                          <td className="py-1 pr-2" title={`${s.service} · ${s.usage_type}`}>{s.category}</td>
                          <td className="pr-2" style={muted}>{s.resource || "—"}</td>
                          <td className="pr-2" style={muted}>{s.usage || "—"}</td>
                          <td className="text-right pr-2">{usd.format(Number(s.cost_usd))}</td>
                          <td className="pr-2" style={muted}>
                            {direct > 0 && alloc > 0 ? `direct ${usd.format(direct)} + allocated ${usd.format(alloc)}`
                              : direct > 0 ? "direct (tag)" : "allocated (estimate)"}
                          </td>
                          <td style={muted}>{s.billing_type}</td>
                        </tr>
                      );
                    })}
                    <tr style={{ borderTop: "1px solid var(--border)", fontWeight: 600 }}>
                      <td className="py-1" colSpan={3}>Total attributed to QA</td>
                      <td className="text-right pr-2">{fmt(m.qa_total_usd)}</td>
                      <td colSpan={2} style={muted}>
                        {m.direct_usd != null ? `direct ${fmt(m.direct_usd)}` : ""}
                        {m.direct_usd != null && m.allocated_usd != null ? " · " : ""}
                        {m.allocated_usd != null ? `allocated ${fmt(m.allocated_usd)}` : ""}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <DailyTrend daily={c.daily ?? []} />

          <div className={card} style={cardStyle}>
            <div className="text-sm font-semibold mb-2" style={{ color: "var(--text-2)" }}>Not attributed to QA</div>
            <div className="text-xs mb-2" style={muted}>
              Rest of the AWS account this month: {fmt(m.unattributed_usd)} (account total {fmt(m.account_total_usd)}).
              Production on Lightsail: {fmt(m.production_lightsail_excluded_usd)} — excluded.
              {m.account_credits_usd != null && Number(m.account_credits_usd) !== 0 &&
                ` Credits/refunds applied to the account: ${fmt(m.account_credits_usd)} (net ${fmt(m.account_net_usd)}).`}
            </div>
            {(c.unattributed_by_service ?? []).length > 0 && (
              <table className="w-full text-xs tabular-nums">
                <tbody>
                  {c.unattributed_by_service!.map((r) => (
                    <tr key={r.service} style={{ borderTop: "1px solid var(--border)" }}>
                      <td className="py-1">{r.service}{r.production ? " · Production" : ""}</td>
                      <td className="text-right">{usd.format(Number(r.cost_usd))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="text-[11px] space-y-0.5" style={muted}>
            {c.attribution?.direct_source && <div>Direct: costs tagged {c.attribution.direct_source}.</div>}
            {c.attribution?.allocated_source && (
              <div>
                Allocated (estimate): {c.attribution.allocated_source} not already tagged
                {c.attribution.allocation_verified?.verified
                  ? " — QA is the only EC2 instance in the region."
                  : ` — not verified${c.attribution.allocation_verified?.reason ? ` (${c.attribution.allocation_verified.reason})` : ""}.`}
              </div>
            )}
            <div>
              Billing data through {c.data_through ?? "—"}; AWS updates it several times a day and current-month
              figures stay estimates until the invoice closes.
              {fetched && ` Collected from Cost Explorer ${fetched.toLocaleString()}`}
              {c.interval_seconds ? ` · every ${Math.round(c.interval_seconds / 3600)}h.` : "."}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
