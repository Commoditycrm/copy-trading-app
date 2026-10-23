"use client";

/**
 * Admin → Paper auto-trades. QA automatic Alpaca PAPER trading: the two
 * switches, each listed account's paper checks and order caps, recent orders
 * on those accounts and their copies, and every refusal the guard made.
 * Nothing here places an order; "Verify" is one read-only Alpaca call.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

interface Account {
  id: string;
  missing?: boolean;
  owner?: string;
  owner_role?: string | null;
  broker?: string;
  label?: string;
  is_paper?: boolean;
  connection_status?: string;
  number_masked?: string | null;
  problems: string[];
  identity?: { ok: boolean; problem: string | null; number_masked: string | null; age_s: number } | null;
  counts?: { this_minute: number; today: number; tripped: string | null } | null;
}

interface OrderRow {
  id: string;
  ref: string;
  source: string;
  account_id: string | null;
  symbol: string;
  side: string;
  quantity: string;
  order_type: string;
  status: string;
  reject_reason: string | null;
  created_at: string | null;
}

interface Status {
  env_enabled: boolean;
  toggle_on: boolean;
  effective: boolean;
  limits: { per_minute: number; per_day: number };
  accounts: Account[];
  orders: OrderRow[];
  events: { at: string | null; action: string; account_id: string | null; detail: Record<string, unknown> | null }[];
}

const card = "rounded-xl p-4";
const cardStyle = { background: "var(--panel-2)", border: "1px solid var(--border)" };
const muted = { color: "var(--muted)" };
const good = "#22c55e";
const bad = "#ef4444";
const warn = "#b45309";

function Pill({ on, text }: { on: boolean; text: string }) {
  return (
    <span className="text-xs font-semibold px-2 py-0.5 rounded-full"
          style={{ background: on ? "rgba(34,197,94,0.12)" : "var(--panel)", color: on ? good : "var(--muted)",
                   border: "1px solid var(--border)" }}>
      {text}
    </span>
  );
}

const fmtTime = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");

export default function PaperAutotradePage() {
  const [s, setS] = useState<Status | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setS(await api<Status>("/api/admin/paper-autotrade"));
    } catch (e) {
      notify.fromError(e, "Could not load paper auto-trading status");
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  async function act(key: string, path: string, body?: unknown, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(key);
    try {
      await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      await load();
    } catch (e) {
      notify.fromError(e, "Action refused");
    } finally {
      setBusy(null);
    }
  }

  if (!s) return <div style={muted}>Loading…</div>;

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-bold">Paper auto-trades</h2>
        <p className="text-xs mt-1" style={muted}>
          QA only. Approved Discord alerts and subscriber copies run automatically on the listed Alpaca PAPER
          accounts; each account is proven paper before every order, copies never go to a live account, and
          the Trade Panel stays manual. Orders stay on their owner&apos;s own account.
        </p>
      </div>

      <div className={card} style={cardStyle}>
        <div className="flex flex-wrap items-center gap-2">
          <Pill on={s.env_enabled} text={`Server switch: ${s.env_enabled ? "on" : "off"}`} />
          <Pill on={s.toggle_on} text={`Admin toggle: ${s.toggle_on ? "on" : "off"}`} />
          <Pill on={s.effective} text={s.effective ? "Automation running" : "Automation off"} />
          <span className="text-xs" style={muted}>
            Caps per account: {s.limits.per_minute}/min, {s.limits.per_day}/day
          </span>
          <div className="flex-1" />
          <button
            disabled={!s.env_enabled || busy !== null}
            onClick={() => act("toggle", "/api/admin/paper-autotrade/toggle", { on: !s.toggle_on },
              s.toggle_on ? "Turn automatic paper trading OFF?"
                : "Turn automatic paper trading ON for the listed paper accounts? Approved Discord alerts and copies will be sent to Alpaca paper.")}
            className="text-sm px-3 py-1.5 rounded-lg"
            style={{ background: "var(--panel)", border: "1px solid var(--border)", opacity: !s.env_enabled ? 0.5 : 1 }}
            title={s.env_enabled ? "" : "The server switch (QA_PAPER_AUTOTRADE_ENABLED) is off"}>
            {s.toggle_on ? "Turn off" : "Turn on"}
          </button>
        </div>
        {!s.env_enabled && (
          <div className="text-xs mt-2" style={{ color: warn }}>
            Disabled on this server. Nothing here is automated, and no extra checks run on any account.
          </div>
        )}
      </div>

      <div className={card} style={cardStyle}>
        <div className="text-sm font-semibold mb-2" style={{ color: "var(--text-2)" }}>Listed paper accounts</div>
        {s.accounts.length === 0 ? (
          <div className="text-xs" style={muted}>No accounts listed (QA_PAPER_AUTOTRADE_ACCOUNTS).</div>
        ) : (
          <table className="w-full text-xs tabular-nums">
            <thead>
              <tr style={muted}>
                <th className="text-left py-1 pr-2">Owner</th><th className="text-left pr-2">Account</th>
                <th className="text-left pr-2">Checks</th><th className="text-left pr-2">Alpaca identity</th>
                <th className="text-right pr-2">Orders min / day</th><th />
              </tr>
            </thead>
            <tbody>
              {s.accounts.map((a) => (
                <tr key={a.id} style={{ borderTop: "1px solid var(--border)" }}>
                  <td className="py-1.5 pr-2">{a.owner ?? "—"}<div style={muted}>{a.owner_role}</div></td>
                  <td className="pr-2">
                    {a.broker} {a.is_paper ? "paper" : "LIVE"} · {a.number_masked ?? "—"}
                    <div style={muted}>{a.label} · {a.connection_status}</div>
                  </td>
                  <td className="pr-2" style={{ color: a.problems.length ? bad : good }}>
                    {a.problems.length ? a.problems.join("; ") : "paper ✓ unshared ✓ connected ✓"}
                  </td>
                  <td className="pr-2" style={{ color: a.identity ? (a.identity.ok ? good : bad) : "var(--muted)" }}>
                    {a.identity ? (a.identity.ok ? `confirmed ${a.identity.number_masked} (${a.identity.age_s}s ago)`
                      : a.identity.problem) : "not checked yet"}
                  </td>
                  <td className="pr-2 text-right">
                    {a.counts ? `${a.counts.this_minute} / ${a.counts.today}` : "—"}
                    {a.counts?.tripped && <div style={{ color: bad }}>held: {a.counts.tripped}</div>}
                  </td>
                  <td className="text-right whitespace-nowrap">
                    <button className="text-xs underline mr-2" disabled={busy !== null}
                            onClick={() => act(`v${a.id}`, `/api/admin/paper-autotrade/verify/${a.id}`)}>Verify</button>
                    {a.counts?.tripped && (
                      <button className="text-xs underline" disabled={busy !== null}
                              onClick={() => act(`r${a.id}`, `/api/admin/paper-autotrade/reset/${a.id}`, undefined,
                                "Release this account's order cap?")}>Reset cap</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className={card} style={cardStyle}>
        <div className="text-sm font-semibold mb-2" style={{ color: "var(--text-2)" }}>Recent orders (listed accounts and their copies)</div>
        {s.orders.length === 0 ? (
          <div className="text-xs" style={muted}>No orders yet.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs tabular-nums">
              <thead>
                <tr style={muted}>
                  <th className="text-left py-1 pr-2">When</th><th className="text-left pr-2">Source</th>
                  <th className="text-left pr-2">Order</th><th className="text-left pr-2">Status</th>
                  <th className="text-left pr-2">Reason</th><th className="text-left">Ref</th>
                </tr>
              </thead>
              <tbody>
                {s.orders.map((o) => (
                  <tr key={o.id} style={{ borderTop: "1px solid var(--border)" }}>
                    <td className="py-1 pr-2 whitespace-nowrap" style={muted}>{fmtTime(o.created_at)}</td>
                    <td className="pr-2">{o.source}</td>
                    <td className="pr-2 whitespace-nowrap">{o.side.toUpperCase()} {o.quantity} {o.symbol} · {o.order_type}</td>
                    <td className="pr-2" style={{ color: o.status === "rejected" ? bad : o.status === "filled" ? good : "var(--text-2)" }}>{o.status}</td>
                    <td className="pr-2" style={muted}><div className="truncate" style={{ maxWidth: 320 }}>{o.reject_reason ?? "—"}</div></td>
                    <td style={muted}>{o.ref}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className={card} style={cardStyle}>
        <div className="text-sm font-semibold mb-2" style={{ color: "var(--text-2)" }}>Guard events</div>
        {s.events.length === 0 ? (
          <div className="text-xs" style={muted}>No refusals, cap trips or toggles recorded.</div>
        ) : (
          <table className="w-full text-xs">
            <tbody>
              {s.events.map((e, i) => (
                <tr key={i} style={{ borderTop: "1px solid var(--border)" }}>
                  <td className="py-1 pr-2 whitespace-nowrap" style={muted}>{fmtTime(e.at)}</td>
                  <td className="pr-2">{e.action.replace("paper_autotrade.", "")}</td>
                  <td style={muted}>{e.detail ? JSON.stringify(e.detail) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
