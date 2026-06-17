"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { PageLoading } from "@/components/PageLoading";
import { SearchableSelect } from "@/components/SearchableSelect";
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

  // What we display in the heading — "Your P&L" or "<sub> · P&L"
  const viewingLabel = useMemo(() => {
    if (!viewingUserId || !user) return "Your P&L";
    const s = subs.find((s) => s.user_id === viewingUserId);
    return s ? `${s.display_name ?? s.email} · P&L` : "Subscriber P&L";
  }, [viewingUserId, user, subs]);

  // Centered loader for the initial mount — once the first month lands,
  // subsequent month-switches keep the grid visible and show the small
  // inline loader at the bottom.
  if (!firstLoadDone) return <PageLoading />;

  // Short month label for the mobile header — "Jun '26" instead of
  // "June 2026" so the row fits inside a 320-360px phone.
  const monthLabelShort = `${cursor.toLocaleString(undefined, { month: "short" })} '${String(cursor.getFullYear()).slice(-2)}`;
  const monthLabelLong = cursor.toLocaleString(undefined, { month: "long", year: "numeric" });
  // Compact dollar formatter for cells on phones (40-50px wide). We
  // round to the dollar and use $1.2K / $-235 style so 5+ digit P&Ls
  // don't overflow the cell. Desktop keeps full currency formatting.
  const fmtCompact = (n: number) => {
    const abs = Math.abs(n);
    const sign = n < 0 ? "-" : "";
    if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(1)}K`;
    return `${sign}$${Math.round(abs)}`;
  };

  return (
    <div className="space-y-3 md:space-y-4 max-w-5xl">
      <div className="flex items-start md:items-center justify-between flex-wrap gap-2 md:gap-3">
        <div className="min-w-0">
          <h1 className="text-lg md:text-2xl font-semibold truncate">{viewingLabel}</h1>
          <p className="text-xs md:text-sm" style={{ color: "var(--muted)" }}>
            Realized P&amp;L from broker fills.
            {syncing && <span className="ml-2">Syncing…</span>}
            {syncMsg && <span className="ml-2" style={{ color: "var(--accent)" }}>{syncMsg}</span>}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {user?.role === "trader" && (
            <div className="w-full sm:w-auto sm:min-w-[220px]">
              <SearchableSelect
                value={viewingUserId ?? ""}
                onChange={(v) => setViewingUserId(v || null)}
                options={[
                  { value: "", label: "— My P&L —" },
                  ...subs.map((s) => ({
                    value: s.user_id,
                    label: s.display_name ?? s.email,
                  })),
                ]}
                placeholder="View P&L for"
                style={{ height: 34 }}
              />
            </div>
          )}
          <button
            onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1))}
            className="px-3 py-1 rounded border" style={{ borderColor: "var(--border)" }}
          >
            ‹
          </button>
          <div className="text-center font-medium px-1 min-w-[5rem] md:min-w-[10rem]">
            <span className="md:hidden">{monthLabelShort}</span>
            <span className="hidden md:inline">{monthLabelLong}</span>
          </div>
          <button
            onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1))}
            className="px-3 py-1 rounded border" style={{ borderColor: "var(--border)" }}
          >
            ›
          </button>
        </div>
      </div>

      <div className="text-xs md:text-sm">
        <span style={{ color: "var(--muted)" }}>Month total: </span>
        <span style={{ color: monthTotal >= 0 ? "var(--good)" : "var(--bad)" }}>
          {monthTotal.toLocaleString(undefined, { style: "currency", currency: "USD" })}
        </span>
      </div>

      {/* Day-of-week headers — single letter on mobile so the row doesn't
          overflow the 320-360px phone width. Full names on tablet+. */}
      <div className="grid grid-cols-7 gap-1 text-[10px] md:text-xs" style={{ color: "var(--muted)" }}>
        {[
          { full: "Sun", short: "S" },
          { full: "Mon", short: "M" },
          { full: "Tue", short: "T" },
          { full: "Wed", short: "W" },
          { full: "Thu", short: "T" },
          { full: "Fri", short: "F" },
          { full: "Sat", short: "S" },
        ].map(d => (
          <div key={d.full} className="px-1 md:px-2 py-1 text-center md:text-left">
            <span className="md:hidden">{d.short}</span>
            <span className="hidden md:inline">{d.full}</span>
          </div>
        ))}
      </div>
      <div className="grid grid-cols-7 gap-1">
        {cells.map((d, i) => {
          if (!d) return <div key={i} className="h-16 md:h-24" />;
          const key = iso(d);
          const day = byDay[key];
          const pnl = day ? Number(day.realized_pnl) : 0;
          const has = !!day;
          const onClick = has
            // Show every order (buys + sells) on the picked date, not just
            // the closing legs.
            ? () => router.push(`/trades?from=${key}&to=${key}`)
            : undefined;
          return (
            <button
              key={i}
              type="button"
              onClick={onClick}
              disabled={!has}
              title={has ? `View ${day.trade_count} trade${day.trade_count === 1 ? "" : "s"} on ${key}` : undefined}
              className="h-16 md:h-24 p-1 md:p-2 rounded border flex flex-col text-left transition-colors"
              style={{
                borderColor: "var(--border)",
                background: has ? (pnl >= 0 ? "rgba(34,197,94,0.08)" : "rgba(239,68,68,0.08)") : "var(--panel)",
                cursor: has ? "pointer" : "default",
              }}
            >
              <div className="text-[10px] md:text-xs" style={{ color: "var(--muted)" }}>{d.getDate()}</div>
              {has && (
                <>
                  <div
                    className="mt-auto font-medium tabular-nums leading-tight text-[10px] md:text-sm"
                    style={{ color: pnl >= 0 ? "var(--good)" : "var(--bad)" }}
                  >
                    {/* Compact dollar on phones (cell is ~45px wide);
                        full currency on tablet+. */}
                    <span className="md:hidden">{fmtCompact(pnl)}</span>
                    <span className="hidden md:inline">
                      {pnl.toLocaleString(undefined, { style: "currency", currency: "USD" })}
                    </span>
                  </div>
                  {/* Trade count is informative but optional — drop it on
                      phones so the cell stays uncluttered. */}
                  <div className="hidden md:block text-xs" style={{ color: "var(--muted)" }}>
                    {day.trade_count} trade{day.trade_count === 1 ? "" : "s"}
                  </div>
                </>
              )}
            </button>
          );
        })}
      </div>
      {loading && <p style={{ color: "var(--muted)" }}>Loading…</p>}
    </div>
  );
}
