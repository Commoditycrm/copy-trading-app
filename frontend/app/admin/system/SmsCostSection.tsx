"use client";

/**
 * Admin System → "SMS — Twilio usage & cost". Twilio's own billing for the
 * account (all environments using these credentials). Kept apart from the AWS
 * (infrastructure) costs above.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

interface Day { date: string; messages: number; segments: number; cost_usd: string }
interface SmsCosts {
  status: "ok" | "unavailable";
  reason?: string;
  currency?: string;
  scope?: string;
  month?: string;
  messages?: number;
  segments?: number;
  cost_usd?: string;
  last_month?: { messages: number; cost_usd: string; month: string };
  daily?: Day[];
  fetched_at?: string;
  cached_for_s?: number;
  last_error?: string;
}

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4 });
const fmt = (v: string | null | undefined) => (v == null ? "Unavailable" : usd.format(Number(v)));
const card = "rounded-xl p-4";
const cardStyle = { background: "var(--panel-2)", border: "1px solid var(--border)" };
const muted = { color: "var(--muted)" };
const warn = "#b45309";

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className={card} style={cardStyle}>
      <div className="text-xs" style={muted}>{label}</div>
      <div className="text-lg font-semibold mt-1 tabular-nums">{value}</div>
      {sub && <div className="text-xs mt-1" style={muted}>{sub}</div>}
    </div>
  );
}

export default function SmsCostSection() {
  const [c, setC] = useState<SmsCosts | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (refresh = false) => {
    setBusy(true);
    try {
      setC(await api<SmsCosts>(`/api/admin/system/sms-costs${refresh ? "?refresh=true" : ""}`));
    } catch (e) {
      if (refresh) notify.fromError(e, "Could not load SMS costs");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-bold">SMS — Twilio usage &amp; cost</h3>
          <p className="text-xs mt-0.5" style={muted}>Messaging spend from Twilio. Separate from AWS infrastructure costs.</p>
        </div>
        <button onClick={() => load(true)} disabled={busy} className="text-sm px-3 py-1.5 rounded-lg"
                style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)", opacity: busy ? 0.6 : 1 }}>
          {busy ? "Loading…" : "Reload"}
        </button>
      </div>

      {c?.status === "unavailable" && (
        <div className={card} style={{ ...cardStyle, border: `1px solid ${warn}` }}>
          <div className="text-sm font-semibold" style={{ color: warn }}>SMS costs unavailable</div>
          <div className="text-xs mt-1" style={muted}>{c.reason}</div>
        </div>
      )}

      {c?.status === "ok" && (
        <>
          <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}>
            <Stat label={`SMS cost, ${c.month}`} value={fmt(c.cost_usd)} sub={`${c.messages} texts · ${c.segments} segments`} />
            <Stat label={`Last month (${c.last_month?.month || "—"})`} value={fmt(c.last_month?.cost_usd)} sub={`${c.last_month?.messages ?? 0} texts`} />
          </div>

          <div className={card} style={cardStyle}>
            <div className="text-sm font-semibold mb-2" style={{ color: "var(--text-2)" }}>Daily spend ({c.month})</div>
            {!c.daily?.length ? <div className="text-xs" style={muted}>No texts this month.</div> : (
              <table className="w-full text-xs tabular-nums">
                <thead><tr style={muted}><th className="text-left py-1">Date</th><th className="text-right">Texts</th><th className="text-right">Segments</th><th className="text-right">Cost</th></tr></thead>
                <tbody>
                  {[...c.daily].reverse().filter((x) => x.messages > 0 || Number(x.cost_usd) > 0).map((x) => (
                    <tr key={x.date} style={{ borderTop: "1px solid var(--border)" }}>
                      <td className="py-1">{x.date}</td><td className="text-right">{x.messages}</td>
                      <td className="text-right">{x.segments}</td><td className="text-right">{fmt(x.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="text-[11px] space-y-0.5" style={muted}>
            <div>{c.scope}: Twilio&apos;s billing (Usage Records, SMS only).</div>
            <div>Last updated {c.fetched_at ? new Date(c.fetched_at).toLocaleString() : "—"}{c.cached_for_s ? ` (cached ${Math.round(c.cached_for_s / 60)} min)` : ""}.{c.last_error ? ` ${c.last_error}.` : ""}</div>
          </div>
        </>
      )}
    </div>
  );
}
