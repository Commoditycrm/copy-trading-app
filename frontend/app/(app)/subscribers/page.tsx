"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { SubscriberSummary } from "@/lib/types";

// Drop trailing zeros from the backend's "1.300" → "1.3", "1.000" → "1".
const fmtMultiplier = (v: string): string => {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n.toString() : v;
};

export default function SubscribersPage() {
  const [rows, setRows] = useState<SubscriberSummary[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState<Record<string, { multiplier: string }>>({});

  async function load() {
    try { setRows(await api<SubscriberSummary[]>("/api/subscribers")); }
    catch (e) { setErr(e instanceof ApiError ? String(e.detail) : String(e)); }
  }
  useEffect(() => { load(); }, []);

  async function save(id: string) {
    const cur = editing[id];
    if (!cur) return;
    // Round to 1 decimal so manual typing of "1.234" cleans to "1.2"
    // before hitting the API. Matches the subscriber's own settings page.
    const n = Number(cur.multiplier);
    if (!Number.isFinite(n) || n <= 0 || n > 100) {
      setErr("multiplier must be between 0.1 and 100");
      return;
    }
    const rounded = (Math.round(n * 10) / 10).toFixed(1);
    await api(`/api/subscribers/${id}/multiplier`, {
      method: "PATCH",
      body: JSON.stringify({ multiplier: rounded }),
    });
    setEditing(prev => { const n = {...prev}; delete n[id]; return n; });
    load();
  }

  if (err) return <p style={{color: "var(--bad)"}}>{err}</p>;

  return (
    <div className="space-y-4 max-w-6xl">
      <h1 className="text-2xl font-semibold">Subscribers</h1>
      <p className="text-sm" style={{color: "var(--muted)"}}>People currently following you. Adjust their multiplier to change copy size.</p>
      <div className="overflow-x-auto rounded border" style={{borderColor: "var(--border)"}}>
        <table className="w-full text-sm">
          <thead style={{background: "var(--panel)"}}>
            <tr>
              {["Subscriber", "Copy", "Multiplier", "Brokers", "30d realized P&L", ""].map(h =>
                <th key={h} className="text-left px-3 py-2 font-medium" style={{color: "var(--muted)"}}>{h}</th>
              )}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={6} className="px-3 py-6 text-center" style={{color: "var(--muted)"}}>No subscribers yet.</td></tr>
            )}
            {rows.map(r => {
              const ed = editing[r.user_id];
              const pnl = Number(r.realized_pnl_30d);
              return (
                <tr key={r.user_id} className="border-t" style={{borderColor: "var(--border)"}}>
                  <td className="px-3 py-2">
                    <div>{r.display_name ?? r.email}</div>
                    <div className="text-xs" style={{color: "var(--muted)"}}>{r.email}</div>
                  </td>
                  <td className="px-3 py-2">
                    <span style={{color: r.copy_enabled ? "var(--good)" : "var(--muted)"}}>
                      {r.copy_enabled ? "ON" : "OFF"}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    {ed ? (
                      <input
                        type="number" step="0.1" min="0.1" max="100"
                        className="w-20 p-1 rounded bg-transparent border" style={{borderColor: "var(--border)"}}
                        value={ed.multiplier}
                        onChange={e => setEditing(p => ({...p, [r.user_id]: {...ed, multiplier: e.target.value}}))}
                      />
                    ) : <>×{fmtMultiplier(r.multiplier)}</>}
                  </td>
                  <td className="px-3 py-2">{r.broker_count}</td>
                  <td className="px-3 py-2" style={{color: pnl >= 0 ? "var(--good)" : "var(--bad)"}}>
                    {pnl.toLocaleString(undefined, { style: "currency", currency: "USD" })}
                  </td>
                  <td className="px-3 py-2">
                    {ed ? (
                      <div className="flex gap-2">
                        <button onClick={() => save(r.user_id)} className="px-3 py-1 text-sm rounded" style={{background: "var(--accent)", color: "#06121f"}}>Save</button>
                        <button onClick={() => setEditing(p => { const n = {...p}; delete n[r.user_id]; return n; })} className="px-3 py-1 text-sm rounded border" style={{borderColor: "var(--border)"}}>Cancel</button>
                      </div>
                    ) : (
                      <button onClick={() => setEditing(p => ({...p, [r.user_id]: { multiplier: parseFloat(r.multiplier).toString() }}))} className="px-3 py-1 text-sm rounded border" style={{borderColor: "var(--border)"}}>Edit</button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
