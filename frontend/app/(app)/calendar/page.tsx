"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { DailyPnL, SubscriberSummary, User } from "@/lib/types";

function startOfMonth(d: Date) { return new Date(d.getFullYear(), d.getMonth(), 1); }
function endOfMonth(d: Date) { return new Date(d.getFullYear(), d.getMonth() + 1, 0); }
function iso(d: Date) { return d.toISOString().slice(0, 10); }

export default function CalendarPage() {
  const [cursor, setCursor] = useState(() => startOfMonth(new Date()));
  const [data, setData] = useState<DailyPnL[]>([]);
  const [loading, setLoading] = useState(true);
  const [user, setUser] = useState<User | null>(null);
  const [subs, setSubs] = useState<SubscriberSummary[]>([]);
  // The "viewing" user — defaults to self. Trader can pick a subscriber.
  const [viewingUserId, setViewingUserId] = useState<string | null>(null);
  // Sync status — auto-sync fills on mount.
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);

  const range = useMemo(() => ({ from: iso(startOfMonth(cursor)), to: iso(endOfMonth(cursor)) }), [cursor]);

  const loadPnL = useCallback(() => {
    setLoading(true);
    const qs = viewingUserId ? `&user_id=${viewingUserId}` : "";
    api<DailyPnL[]>(`/api/calendar/pnl?from=${range.from}&to=${range.to}${qs}`)
      .then(setData)
      .finally(() => setLoading(false));
  }, [range.from, range.to, viewingUserId]);

  // Auto-sync fills on first load, then load P&L.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const u = await api<User>("/api/auth/me");
        if (cancelled) return;
        setUser(u);
        // Only the trader gets the subscriber dropdown.
        if (u.role === "trader") {
          api<SubscriberSummary[]>("/api/subscribers").then((rows) => { if (!cancelled) setSubs(rows); });
        }
        // Sync our own fills — refreshes the data the calendar reads from.
        setSyncing(true);
        try {
          const res = await api<{ fills_added: number; orders_added: number }>(
            "/api/trades/sync-fills", { method: "POST" }
          );
          if (!cancelled && (res.fills_added || res.orders_added)) {
            setSyncMsg(`Synced ${res.fills_added} new fill${res.fills_added === 1 ? "" : "s"}.`);
            setTimeout(() => setSyncMsg(null), 4000);
          }
        } catch { /* sync failures are non-blocking — P&L can still render from existing data */ }
        finally { if (!cancelled) setSyncing(false); }
      } catch { /* auth issues handled by AppShell */ }
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => { loadPnL(); }, [loadPnL]);

  const byDay = useMemo(() => {
    const m: Record<string, DailyPnL> = {};
    for (const d of data) m[d.day] = d;
    return m;
  }, [data]);

  const cells: (Date | null)[] = [];
  const first = startOfMonth(cursor);
  const lead = first.getDay();
  for (let i = 0; i < lead; i++) cells.push(null);
  const last = endOfMonth(cursor);
  for (let d = 1; d <= last.getDate(); d++) cells.push(new Date(cursor.getFullYear(), cursor.getMonth(), d));
  while (cells.length % 7 !== 0) cells.push(null);

  const monthTotal = data.reduce((s, d) => s + Number(d.realized_pnl), 0);

  // What we display in the heading — "Your P&L" or "<sub> · P&L"
  const viewingLabel = useMemo(() => {
    if (!viewingUserId || !user) return "Your P&L";
    const s = subs.find((s) => s.user_id === viewingUserId);
    return s ? `${s.display_name ?? s.email} · P&L` : "Subscriber P&L";
  }, [viewingUserId, user, subs]);

  return (
    <div className="space-y-4 max-w-5xl">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-semibold">{viewingLabel}</h1>
          <p className="text-sm" style={{ color: "var(--muted)" }}>
            Realized P&amp;L from broker fills.
            {syncing && <span className="ml-2">Syncing…</span>}
            {syncMsg && <span className="ml-2" style={{ color: "var(--accent)" }}>{syncMsg}</span>}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {user?.role === "trader" && (
            <select
              value={viewingUserId ?? ""}
              onChange={(e) => setViewingUserId(e.target.value || null)}
              className="p-2 rounded bg-transparent border text-sm"
              style={{ borderColor: "var(--border)" }}
              title="View P&L for"
            >
              <option value="">— My P&amp;L —</option>
              {subs.map((s) => (
                <option key={s.user_id} value={s.user_id}>
                  {s.display_name ?? s.email}
                </option>
              ))}
            </select>
          )}
          <button
            onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1))}
            className="px-3 py-1 rounded border" style={{ borderColor: "var(--border)" }}
          >
            ‹
          </button>
          <div className="min-w-[10rem] text-center font-medium">
            {cursor.toLocaleString(undefined, { month: "long", year: "numeric" })}
          </div>
          <button
            onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1))}
            className="px-3 py-1 rounded border" style={{ borderColor: "var(--border)" }}
          >
            ›
          </button>
        </div>
      </div>

      <div className="text-sm">
        <span style={{ color: "var(--muted)" }}>Month total: </span>
        <span style={{ color: monthTotal >= 0 ? "var(--good)" : "var(--bad)" }}>
          {monthTotal.toLocaleString(undefined, { style: "currency", currency: "USD" })}
        </span>
      </div>

      <div className="grid grid-cols-7 gap-1 text-xs" style={{ color: "var(--muted)" }}>
        {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map(d => <div key={d} className="px-2 py-1">{d}</div>)}
      </div>
      <div className="grid grid-cols-7 gap-1">
        {cells.map((d, i) => {
          if (!d) return <div key={i} className="h-24" />;
          const key = iso(d);
          const day = byDay[key];
          const pnl = day ? Number(day.realized_pnl) : 0;
          const has = !!day;
          return (
            <div key={i} className="h-24 p-2 rounded border flex flex-col" style={{
              borderColor: "var(--border)",
              background: has ? (pnl >= 0 ? "rgba(34,197,94,0.08)" : "rgba(239,68,68,0.08)") : "var(--panel)",
            }}>
              <div className="text-xs" style={{ color: "var(--muted)" }}>{d.getDate()}</div>
              {has && (
                <>
                  <div className="mt-auto font-medium" style={{ color: pnl >= 0 ? "var(--good)" : "var(--bad)" }}>
                    {pnl.toLocaleString(undefined, { style: "currency", currency: "USD" })}
                  </div>
                  <div className="text-xs" style={{ color: "var(--muted)" }}>{day.trade_count} trade{day.trade_count === 1 ? "" : "s"}</div>
                </>
              )}
            </div>
          );
        })}
      </div>
      {loading && <p style={{ color: "var(--muted)" }}>Loading…</p>}
    </div>
  );
}
