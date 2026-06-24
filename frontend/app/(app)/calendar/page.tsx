"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { PageLoading } from "@/components/PageLoading";
import { SearchableSelect } from "@/components/SearchableSelect";
import { fmtSignedUsd } from "@/lib/format";
import type { DailyPnL, SubscriberSummary, User } from "@/lib/types";

function startOfMonth(d: Date) { return new Date(d.getFullYear(), d.getMonth(), 1); }
function endOfMonth(d: Date) { return new Date(d.getFullYear(), d.getMonth() + 1, 0); }
/** Local-date string. `toISOString()` is UTC and shifts the date for users
 *  east/west of UTC — that's why a cell labeled "18" was getting key "17". */
function iso(d: Date) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}
/** US trading app — bucket daily P&L by US Eastern (America/New_York), the
 *  market's calendar, regardless of the viewer's local timezone. */
function browserTz(): string {
  return "America/New_York";
}

export default function CalendarPage() {
  const router = useRouter();
  const [cursor, setCursor] = useState(() => startOfMonth(new Date()));
  const [data, setData] = useState<DailyPnL[]>([]);
  const [loading, setLoading] = useState(true);
  // Tracks whether the FIRST month's P&L fetch has completed. Subsequent
  // month switches show the small inline loader without remounting the
  // whole grid; only the initial mount surfaces the centered loading.
  const [firstLoadDone, setFirstLoadDone] = useState(false);
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
    api<DailyPnL[]>(`/api/calendar/pnl?from=${range.from}&to=${range.to}&tz=${encodeURIComponent(browserTz())}${qs}`)
      .then(setData)
      .finally(() => {
        setLoading(false);
        setFirstLoadDone(true);
      });
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
  const tradingDays = data.filter(d => d.trade_count > 0).length;
  // Heatmap intensity is scaled to the month's largest absolute day.
  const maxAbs = Math.max(...data.map(d => Math.abs(Number(d.realized_pnl))), 1);
  const todayKey = iso(new Date());

  // What we display in the heading — "Your P&L" or "<sub> · P&L"
  const viewingLabel = useMemo(() => {
    if (!viewingUserId || !user) return "P&L Calendar";
    const s = subs.find((s) => s.user_id === viewingUserId);
    return s ? `${s.display_name ?? s.email} · P&L` : "Subscriber P&L";
  }, [viewingUserId, user, subs]);

  // Centered loader for the initial mount.
  if (!firstLoadDone) return <PageLoading />;

  return (
    <div className="max-w-5xl">
      {/* Month bar: total + nav */}
      <div className="card p-4 mb-4 flex items-center justify-between gap-3 flex-wrap" style={{ borderRadius: 10 }}>
        <div className="flex items-center gap-5">
          <div>
            <div className="text-[11px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Month total</div>
            <div className="num num-lg" style={{ color: monthTotal > 0 ? "var(--good)" : monthTotal < 0 ? "var(--bad)" : "var(--text)" }}>
              {fmtSignedUsd(monthTotal)}
            </div>
          </div>
          <span className="h-9 w-px" style={{ background: "var(--border)" }} aria-hidden />
          <div>
            <div className="text-[11px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Trading days</div>
            <div className="num num-lg" style={{ color: "var(--text)" }}>{tradingDays}</div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1))}
            className="btn-ghost grid place-items-center" style={{ width: 34, height: 34 }}
            aria-label="Previous month"
          >
            <ChevronLeft size={16} />
          </button>
          <div className="min-w-[10rem] text-center font-semibold" style={{ color: "var(--text)" }}>
            {cursor.toLocaleString(undefined, { month: "long", year: "numeric" })}
          </div>
          <button
            onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1))}
            className="btn-ghost grid place-items-center" style={{ width: 34, height: 34 }}
            aria-label="Next month"
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </div>

      <div className="grid grid-cols-7 gap-1.5 text-[11px] font-medium mb-1.5" style={{ color: "var(--muted)" }}>
        {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map(d => <div key={d} className="px-2">{d}</div>)}
      </div>
      <div className="grid grid-cols-7 gap-1.5">
        {cells.map((d, i) => {
          if (!d) return <div key={i} className="h-24" />;
          const key = iso(d);
          const day = byDay[key];
          const pnl = day ? Number(day.realized_pnl) : 0;
          const has = !!day;
          const isToday = key === todayKey;
          // Heatmap fill — green for gains / red for losses, opacity scaled to
          // the day's magnitude vs. the month's biggest move.
          const intensity = has ? 0.12 + 0.5 * (Math.abs(pnl) / maxAbs) : 0;
          const bg = has
            ? (pnl >= 0 ? `rgba(34,197,94,${intensity})` : `rgba(239,68,68,${intensity})`)
            : "var(--panel)";
          return (
            <motion.button
              key={i}
              type="button"
              onClick={has ? () => router.push(`/trades?from=${key}&to=${key}`) : undefined}
              disabled={!has}
              title={has ? `View ${day.trade_count} trade${day.trade_count === 1 ? "" : "s"} on ${key}` : undefined}
              whileHover={has ? { y: -2 } : undefined}
              transition={{ duration: 0.15 }}
              className="h-24 p-2 border flex flex-col text-left"
              style={{
                borderRadius: 10,
                borderColor: isToday ? "var(--accent)" : "var(--border)",
                boxShadow: isToday ? "0 0 0 1px var(--accent)" : "none",
                background: bg,
                cursor: has ? "pointer" : "default",
              }}
            >
              <div className="text-xs font-medium" style={{ color: isToday ? "var(--accent)" : "var(--muted)" }}>
                {d.getDate()}
              </div>
              {has && (
                <>
                  <div className="mt-auto num font-semibold text-sm" style={{ color: pnl >= 0 ? "var(--good)" : "var(--bad)" }}>
                    {fmtSignedUsd(pnl)}
                  </div>
                  <div className="text-[11px]" style={{ color: "var(--muted)" }}>
                    {day.trade_count} trade{day.trade_count === 1 ? "" : "s"}
                  </div>
                </>
              )}
            </motion.button>
          );
        })}
      </div>
      {loading && <p className="mt-3 text-sm" style={{ color: "var(--muted)" }}>Loading…</p>}
    </div>
  );
}
