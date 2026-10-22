"use client";

/**
 * Admin System → "AWS Testing — Cost & Usage". QA (EC2) resources only: the
 * host collector discovers them and drops anything it can't tie to QA, so
 * nothing account-wide or production is ever shown here. Figures are a
 * snapshot taken every 8h; reloading is free and never rides the 3s CPU poll.
 * Unknown figures render as "Unavailable", never as $0.
 */
import { Fragment, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

interface Item {
  usage_type: string;
  description: string;
  regions: string[];
  quantity: string;
  unit: string;
  cost_usd: string;
  charge_type: string;
}

interface Category {
  category: string;
  cost_usd: string;
  quantity: string | null;
  unit: string | null;
  items: Item[];
}

interface Warning {
  code: string;
  period: string;
  excluded_usd: string;
  from: string;
  to: string;
  categories: string[];
  message: string;
}

interface Period {
  month: string;
  start: string;
  data_through: string | null;
  qa_total_usd: string;
  excluded_usd: string;
  categories: Category[];
  daily: { date: string; cost_usd: string; excluded_usd: string }[];
  warnings: Warning[];
  reconciliation: { matches_cost_explorer: boolean | null; balanced: boolean };
}

interface CostPayload {
  status: "ok" | "unavailable";
  reason?: string;
  error_code?: string;
  action_needed?: string;
  required_permissions?: string[];
  region?: string;
  basis?: string;
  environment?: {
    instances: { id: string; type: string; state: string; public_ipv4: boolean }[];
    volumes: { id: string; type: string; size_gib: number }[];
    public_ipv4: number;
    snapshots: number | null;
    unreadable: string[];
  };
  attribution?: { mode: "inventory" | "tag"; note: string | null; complete: boolean; warnings: Warning[] };
  periods?: Period[];
  run_rate?: {
    available: boolean;
    reason: string | null;
    daily_usd: string | null;
    components: { category: string; basis: string; daily_usd: string }[];
  };
  projection?: { month_usd: string | null; actual_usd?: string; remaining_days?: number; reason: string | null };
  fetched_at?: string;
  last_attempt_at?: string;
  last_error?: { error_code: string; reason: string; action_needed: string } | null;
  interval_seconds?: number;
  age_seconds?: number | null;
  stale?: boolean;
  billing_stale?: boolean;
  billing_lag_days?: number | null;
}

const POLL_MS = 30 * 60 * 1000;
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const fmt = (v: string | null | undefined) => (v == null ? "Unavailable" : usd.format(Number(v)));
// sub-cent rows would all read $0.00; say so instead
const fmtSmall = (v: string) => (Number(v) > 0 && Number(v) < 0.005 ? "< $0.01" : usd.format(Number(v)));
const qty = (q: string | null, unit: string | null) =>
  q == null || unit == null ? "—" : `${Number(q).toLocaleString("en-US", { maximumFractionDigits: 3 })} ${unit}`;
const monthName = (m: string) =>
  new Date(`${m}-01T00:00:00Z`).toLocaleString("en-US", { month: "long", year: "numeric", timeZone: "UTC" });
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

function ServicesTable({ p }: { p: Period }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  if (p.categories.length === 0) {
    return <div className="text-xs" style={muted}>No QA usage in {monthName(p.month)}.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs tabular-nums">
        <thead>
          <tr style={muted}>
            <th className="text-left py-1 pr-2">Service</th>
            <th className="text-right pr-2">Usage</th>
            <th className="text-right">Cost</th>
          </tr>
        </thead>
        <tbody>
          {p.categories.map((c) => {
            const expandable = c.items.length > 1 || c.items[0]?.description !== c.items[0]?.usage_type;
            const isOpen = !!open[c.category];
            return (
              <Fragment key={c.category}>
                <tr style={{ borderTop: "1px solid var(--border)" }}>
                  <td className="py-1.5 pr-2">
                    {expandable ? (
                      <button className="text-left" aria-expanded={isOpen}
                              onClick={() => setOpen((o) => ({ ...o, [c.category]: !isOpen }))}>
                        <span className="inline-block w-3" style={muted}>{isOpen ? "▾" : "▸"}</span>{c.category}
                      </button>
                    ) : (
                      <span><span className="inline-block w-3" />{c.category}</span>
                    )}
                  </td>
                  <td className="text-right pr-2 whitespace-nowrap" style={muted}>{qty(c.quantity, c.unit)}</td>
                  <td className="text-right">{fmt(c.cost_usd)}</td>
                </tr>
                {isOpen && c.items.map((i) => (
                  <tr key={`${c.category}|${i.description}|${i.unit}`} style={muted}>
                    <td className="py-0.5 pr-2 pl-5" title={i.usage_type}>
                      {i.description}
                      {i.regions.length > 0 && (
                        <span title={i.regions.join(", ")}>
                          {" "}({i.regions.length > 2 ? `${i.regions.length} regions` : i.regions.join(", ")})
                        </span>
                      )}
                      <span> · {i.charge_type}</span>
                    </td>
                    <td className="text-right pr-2 whitespace-nowrap">{qty(i.quantity, i.unit)}</td>
                    <td className="text-right">{fmtSmall(i.cost_usd)}</td>
                  </tr>
                ))}
              </Fragment>
            );
          })}
          <tr style={{ borderTop: "1px solid var(--border)", fontWeight: 600 }}>
            <td className="py-1.5" colSpan={2}>Total QA</td>
            <td className="text-right">{fmt(p.qa_total_usd)}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

/** One series → no legend; the heading names it. Bars round only at the top,
 *  2px gaps, a recessive baseline, hover tooltip, and a table view. */
function DailyTrend({ p }: { p: Period }) {
  const [hover, setHover] = useState<number | null>(null);
  const [asTable, setAsTable] = useState(false);
  const daily = p.data_through ? p.daily.filter((d) => d.date <= p.data_through!) : [];
  const vals = daily.map((d) => Number(d.cost_usd));
  const max = Math.max(...vals, 0.01);
  const W = 640, H = 140, gap = 2;
  const bw = Math.max(2, (W - gap * (daily.length - 1)) / Math.max(daily.length, 1));
  return (
    <div className={card} style={cardStyle}>
      <div className="flex items-baseline justify-between mb-2">
        <div className="text-sm font-semibold" style={{ color: "var(--text-2)" }}>Daily QA cost</div>
        <button className="text-xs underline" style={muted} onClick={() => setAsTable((v) => !v)}>
          {asTable ? "Show chart" : "Show table"}
        </button>
      </div>
      {daily.length === 0 ? (
        <div className="text-xs" style={muted}>No billed days yet.</div>
      ) : asTable ? (
        <table className="w-full text-xs tabular-nums">
          <thead><tr style={muted}><th className="text-left py-1">Date</th><th className="text-right">QA cost</th></tr></thead>
          <tbody>
            {daily.map((d) => (
              <tr key={d.date} style={{ borderTop: "1px solid var(--border)" }}>
                <td className="py-1">{d.date}</td>
                <td className="text-right">{usd.format(Number(d.cost_usd))}</td>
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
              const h = vals[i] > 0 ? Math.max(1, (vals[i] / max) * (H - 16)) : 0;
              const x = i * (bw + gap);
              const y = H - h;
              const r = Math.min(4, bw / 2, h);
              const path = `M${x},${H} V${y + r} Q${x},${y} ${x + r},${y} H${x + bw - r} Q${x + bw},${y} ${x + bw},${y + r} V${H} Z`;
              return (
                <g key={d.date} onMouseEnter={() => setHover(i)}>
                  {/* hit target taller than the bar */}
                  <rect x={x} y={0} width={bw + gap} height={H} fill="transparent" />
                  {h > 0 && <path d={path} fill="var(--accent)" opacity={hover == null || hover === i ? 1 : 0.35} />}
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
              {daily[hover].date} · {usd.format(vals[hover])}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function environmentLine(c: CostPayload): string {
  const e = c.environment;
  if (!e) return "";
  const parts = [
    ...e.instances.map((i) => `${i.type} (${i.state})`),
    ...e.volumes.map((v) => `${v.size_gib} GiB ${v.type}`),
    e.public_ipv4 ? `${e.public_ipv4} public IPv4` : "",
    c.region ?? "",
  ];
  return parts.filter(Boolean).join(" · ");
}

export default function AwsCostSection() {
  const [c, setC] = useState<CostPayload | null>(null);
  const [err, setErr] = useState(false);
  const [busy, setBusy] = useState(false);
  const [periodIdx, setPeriodIdx] = useState(0);

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

  const periods = c?.periods ?? [];
  const p = periods[periodIdx];
  const isCurrent = periodIdx === 0;
  const warnings = (c?.attribution?.warnings ?? []).filter((w) => p && w.period === p.month);
  const unreadable = c?.environment?.unreadable ?? [];
  const fetched = c?.fetched_at ? new Date(c.fetched_at) : null;
  const pj = c?.projection;
  const rr = c?.run_rate;

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h3 className="text-lg font-bold">AWS Testing — Cost &amp; Usage</h3>
          <p className="text-xs mt-0.5" style={muted}>
            QA environment only{c?.environment ? `: ${environmentLine(c)}` : ""}.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {periods.length > 1 && (
            <div className="flex rounded-lg overflow-hidden text-xs" style={{ border: "1px solid var(--border)" }}>
              {periods.map((x, i) => (
                <button key={x.month} onClick={() => setPeriodIdx(i)} aria-pressed={i === periodIdx}
                        className="px-2.5 py-1.5"
                        style={{ background: i === periodIdx ? "var(--panel)" : "var(--panel-2)",
                                 color: i === periodIdx ? "var(--text)" : "var(--muted)" }}>
                  {monthName(x.month)}
                </button>
              ))}
            </div>
          )}
          <button onClick={() => load(true)} disabled={busy}
                  className="text-sm px-3 py-1.5 rounded-lg"
                  style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)", opacity: busy ? 0.6 : 1 }}>
            {busy ? "Loading…" : "Reload"}
          </button>
        </div>
      </div>

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
        </div>
      )}

      {c?.status === "ok" && p && (
        <>
          {(c.stale || c.billing_stale || c.last_error) && (
            <div className="rounded-xl p-3 text-xs space-y-0.5" style={{ ...cardStyle, border: `1px solid ${warn}`, color: warn }}>
              {c.stale && (
                <div>
                  These figures are out of date{c.age_seconds != null ? ` (last updated ${Math.round(c.age_seconds / 3600)}h ago)` : ""} —
                  the cost collector on the QA host has missed its runs.
                </div>
              )}
              {c.billing_stale && (
                <div>AWS billing data is {c.billing_lag_days ?? "several"} days behind; recent costs are missing.</div>
              )}
              {c.last_error && <div>Last update failed: {c.last_error.reason}. {c.last_error.action_needed}</div>}
            </div>
          )}

          {(warnings.length > 0 || (isCurrent && unreadable.length > 0)) && (
            <div className="rounded-xl p-3 text-xs" style={{ ...cardStyle, border: `1px solid ${warn}` }}>
              <div className="font-semibold" style={{ color: warn }}>
                Attribution incomplete{Number(p.excluded_usd) > 0 ? ` — ${fmt(p.excluded_usd)} excluded from the QA total` : ""}
              </div>
              <ul className="mt-1 space-y-0.5 list-disc pl-4" style={{ color: "var(--text-2)" }}>
                {warnings.map((w) => (
                  <li key={w.code}>
                    {fmt(w.excluded_usd)} {w.categories.join(", ")} ({w.from === w.to ? w.from : `${w.from} – ${w.to}`}): {w.message}.
                  </li>
                ))}
                {isCurrent && unreadable.length > 0 && (
                  <li>The QA instance role can&apos;t read {unreadable.join(", ")}, so those resources can&apos;t be verified.</li>
                )}
              </ul>
            </div>
          )}

          <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))" }}>
            <Stat label={isCurrent ? "QA month-to-date" : `QA total, ${monthName(p.month)}`} value={fmt(p.qa_total_usd)}
                  sub={p.data_through ? `actual, through ${p.data_through}` : "no billed days"} />
            {isCurrent && (
              <>
                <Stat label="Projected this month (estimate)" value={fmt(pj?.month_usd)}
                      sub={pj?.month_usd != null
                        ? `${fmt(pj.actual_usd)} actual + run rate × ${pj.remaining_days} day${pj.remaining_days === 1 ? "" : "s"}`
                        : pj?.reason ?? undefined} />
                <Stat label="Current daily run rate" value={fmt(rr?.daily_usd)}
                      sub={rr?.available
                        ? rr.components.filter((x) => Number(x.daily_usd) > 0).map((x) => x.category).join(" + ")
                        : rr?.reason ?? undefined} />
              </>
            )}
          </div>

          <div className={card} style={cardStyle}>
            <div className="text-sm font-semibold mb-2" style={{ color: "var(--text-2)" }}>
              QA services — {monthName(p.month)}
            </div>
            <ServicesTable p={p} />
          </div>

          <DailyTrend p={p} />

          <div className="text-[11px] space-y-0.5" style={muted}>
            <div>
              Billing data through {p.data_through ?? "—"} (AWS updates it several times a day; this month stays an
              estimate until the invoice closes). Last successful update: {fetched ? fetched.toLocaleString() : "—"}
              {c.interval_seconds ? `, refreshed every ${Math.round(c.interval_seconds / 3600)}h` : ""}.
            </div>
            <div>
              {c.basis ? `Unblended cost in USD, ${c.basis}. ` : ""}
              {c.attribution?.mode === "tag"
                ? "QA charges are the ones carrying the QA cost-allocation tag."
                : "QA charges are tied to the instance this runs on and its volumes and addresses, over their lifetimes."}
              {p.reconciliation.matches_cost_explorer ? " Totals reconciled with Cost Explorer." : ""}
            </div>
            {c.attribution?.note && <div>{c.attribution.note}</div>}
            {isCurrent && rr?.available && (
              <div>
                Run rate: {rr.components.map((x) => `${x.category} ${x.basis} ${fmtSmall(x.daily_usd)}/day`).join(" · ")}.
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
