"use client";

/**
 * Admin System → "SMS — Twilio usage & cost". Twilio's own billing for the
 * account (all environments using these credentials) and, separately, the
 * texts this server sent and tracked. The projection is an estimate and says
 * so. Kept apart from the AWS (infrastructure) costs above.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

interface Day { date: string; messages: number; segments: number; cost_usd: string }
interface SmsCosts {
  status: "ok" | "unavailable";
  reason?: string;
  currency?: string;
  twilio_account?: {
    scope: string;
    month: string;
    messages: number;
    segments: number;
    cost_usd: string;
    last_month: { messages: number; cost_usd: string; month: string };
    daily: Day[];
    delivery: { sent: number; delivered: number; undelivered: number; failed: number; in_flight: number;
                segments: number; cost_usd: string; top_errors: { code: number; count: number }[]; truncated: boolean;
                excluded_other_channels?: number };
    unregistered_blocked: number;
  };
  projection?: { estimate_usd: string | null; daily_average_usd?: string; basis: string };
  this_server?: { rejected_trade_texts: number; by_status: Record<string, number>; segments: number; cost: string | null };
  whatsapp?: {
    messages: number;
    cost_usd: string;
    last_month: { messages: number; cost_usd: string; month: string };
    delivery: { sent: number; delivered: number; undelivered: number; failed: number };
  };
  whatsapp_this_server?: { rejected_trade_texts: number; cost: string | null };
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

  const t = c?.twilio_account;
  const d = t?.delivery;
  const errText: Record<number, string> = { 30034: "unregistered A2P number (carrier blocked)", 30003: "unreachable handset",
                                            30005: "unknown handset", 30006: "landline or unreachable", 30007: "carrier filtered", 63112: "WhatsApp account issue" };

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

      {c?.status === "ok" && t && d && (
        <>
          {t.unregistered_blocked > 0 && (
            <div className="rounded-xl p-3 text-xs" style={{ ...cardStyle, border: `1px solid ${warn}`, color: warn }}>
              {t.unregistered_blocked} of {d.sent} texts this month were blocked by carriers as coming from an unregistered
              A2P number (error 30034). They are still billed. The A2P 10DLC campaign needs to be registered and approved.
            </div>
          )}

          <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}>
            <Stat label={`SMS cost, ${t.month} (actual)`} value={fmt(t.cost_usd)} sub={`Twilio billing · ${t.messages} texts · ${t.segments} segments`} />
            <Stat label="Projected this month (estimate)" value={fmt(c.projection?.estimate_usd)} sub={c.projection?.basis} />
            <Stat label={`Last month (${t.last_month.month || "—"})`} value={fmt(t.last_month.cost_usd)} sub={`${t.last_month.messages} texts`} />
            <Stat label="Delivered" value={`${d.delivered} / ${d.sent}`}
                  sub={`${d.undelivered} undelivered · ${d.failed} failed${d.in_flight ? ` · ${d.in_flight} in flight` : ""}`} />
            <Stat label="Rejected-trade texts (this server)" value={String(c.this_server?.rejected_trade_texts ?? 0)}
                  sub={`${c.this_server?.segments ?? 0} segments · ${fmt(c.this_server?.cost)}`} />
          </div>

          {c.whatsapp && (
            <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}>
              <Stat label={`WhatsApp cost, ${t.month} (actual)`} value={fmt(c.whatsapp.cost_usd)}
                    sub={`Meta template + Twilio fees · ${c.whatsapp.messages} messages`} />
              <Stat label={`WhatsApp last month (${c.whatsapp.last_month.month || "—"})`} value={fmt(c.whatsapp.last_month.cost_usd)}
                    sub={`${c.whatsapp.last_month.messages} messages`} />
              <Stat label="WhatsApp delivered" value={`${c.whatsapp.delivery.delivered} / ${c.whatsapp.delivery.sent}`}
                    sub={`${c.whatsapp.delivery.undelivered} undelivered · ${c.whatsapp.delivery.failed} failed`} />
              <Stat label="Rejected-trade WhatsApp (this server)" value={String(c.whatsapp_this_server?.rejected_trade_texts ?? 0)} />
            </div>
          )}

          <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))" }}>
            <div className={card} style={cardStyle}>
              <div className="text-sm font-semibold mb-2" style={{ color: "var(--text-2)" }}>Daily spend ({t.month}, actual)</div>
              {t.daily.length === 0 ? <div className="text-xs" style={muted}>No texts this month.</div> : (
                <table className="w-full text-xs tabular-nums">
                  <thead><tr style={muted}><th className="text-left py-1">Date</th><th className="text-right">Texts</th><th className="text-right">Segments</th><th className="text-right">Cost</th></tr></thead>
                  <tbody>
                    {[...t.daily].reverse().filter((x) => x.messages > 0 || Number(x.cost_usd) > 0).map((x) => (
                      <tr key={x.date} style={{ borderTop: "1px solid var(--border)" }}>
                        <td className="py-1">{x.date}</td><td className="text-right">{x.messages}</td>
                        <td className="text-right">{x.segments}</td><td className="text-right">{fmt(x.cost_usd)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <div className={card} style={cardStyle}>
              <div className="text-sm font-semibold mb-2" style={{ color: "var(--text-2)" }}>Delivery this month</div>
              <table className="w-full text-xs tabular-nums">
                <tbody>
                  {[["Sent (outbound SMS)", d.sent], ["Delivered", d.delivered], ["Undelivered", d.undelivered], ["Failed", d.failed],
                    ["Segments", d.segments]].map(([k, v]) => (
                    <tr key={k as string} style={{ borderTop: "1px solid var(--border)" }}>
                      <td className="py-1">{k}</td><td className="text-right">{v as number}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {d.top_errors.length > 0 && (
                <div className="text-xs mt-2" style={muted}>
                  Errors: {d.top_errors.map((e) => `${e.code}${errText[e.code] ? ` (${errText[e.code]})` : ""} × ${e.count}`).join(" · ")}
                </div>
              )}
              {!!d.excluded_other_channels && (
                <div className="text-xs mt-1" style={muted}>{d.excluded_other_channels} WhatsApp/other-channel messages left out (not SMS).</div>
              )}
              {d.truncated && <div className="text-xs mt-1" style={{ color: warn }}>Only the first 5,000 messages were read.</div>}
            </div>
          </div>

          <div className="text-[11px] space-y-0.5" style={muted}>
            <div>{t.scope}: cost and counts are Twilio&apos;s billing (Usage Records); delivery is from Twilio&apos;s message log. The
              rejected-trade count is what this server sent and tracked. The projection is an estimate.</div>
            <div>Last updated {c.fetched_at ? new Date(c.fetched_at).toLocaleString() : "—"}{c.cached_for_s ? ` (cached ${Math.round(c.cached_for_s / 60)} min)` : ""}.{c.last_error ? ` ${c.last_error}.` : ""}</div>
          </div>
        </>
      )}
    </div>
  );
}
