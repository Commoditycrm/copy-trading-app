"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { ChevronLeft, ChevronRight, Info } from "lucide-react";
import { api } from "@/lib/api";
import { fetchAllSubscribers } from "@/lib/subscribers";
import { notify } from "@/lib/toast";
import { getSnapshot, setSnapshot, USER_SNAPSHOT_KEY } from "@/lib/swrCache";
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

// Snapshot keys — the grid caches only the default view (self, current month).
const CAL_KEY = "calendar:landing";
const CAL_SUBS_KEY = "calendar:subs";

export default function CalendarPage() {
  const router = useRouter();
  const [cursor, setCursor] = useState(() => startOfMonth(new Date()));
  // Seed the current-month self-view grid + roster from the last snapshot so
  // return nav skips the centered loader; the effects below revalidate.
  const _calSnap = getSnapshot<DailyPnL[]>(CAL_KEY);
  const [data, setData] = useState<DailyPnL[]>(_calSnap ?? []);
  const [loading, setLoading] = useState(true);
  // Tracks whether the FIRST month's P&L fetch has completed. Subsequent
  // month switches show the small inline loader without remounting the
  // whole grid; only the initial mount surfaces the centered loading.
  const [firstLoadDone, setFirstLoadDone] = useState(_calSnap !== undefined);
  const [user, setUser] = useState<User | null>(() => getSnapshot<User>(USER_SNAPSHOT_KEY) ?? null);
  const [subs, setSubs] = useState<SubscriberSummary[]>(() => getSnapshot<SubscriberSummary[]>(CAL_SUBS_KEY) ?? []);
  // The "viewing" user — defaults to self. Trader can pick a subscriber.
  const [viewingUserId, setViewingUserId] = useState<string | null>(null);
  // Sync status — auto-sync fills on mount.
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);

  const range = useMemo(() => ({ from: iso(startOfMonth(cursor)), to: iso(endOfMonth(cursor)) }), [cursor]);

  // `silent` = a background live-refresh (today's cell ticks with the market);
  // it must NOT toggle the loader or the grid would flash on every poll.
  const loadPnL = useCallback((silent = false) => {
    if (!silent) setLoading(true);
    const qs = viewingUserId ? `&user_id=${viewingUserId}` : "";
    api<DailyPnL[]>(`/api/calendar/pnl?from=${range.from}&to=${range.to}&tz=${encodeURIComponent(browserTz())}${qs}`)
      .then((rows) => {
        setData(rows);
        // Cache only the default landing (self view, current month) — never a
        // subscriber's view or a scrolled-to month.
        if (!viewingUserId && range.from === iso(startOfMonth(new Date()))) {
          setSnapshot(CAL_KEY, rows);
        }
      })
      .finally(() => {
        if (!silent) {
          setLoading(false);
          setFirstLoadDone(true);
        }
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
        setSnapshot(USER_SNAPSHOT_KEY, u);
        // Only the trader gets the subscriber dropdown.
        if (u.role === "trader") {
          fetchAllSubscribers().then((items) => {
            if (!cancelled) { setSubs(items); setSnapshot(CAL_SUBS_KEY, items); }
          }).catch((e) => notify.fromError(e, "Could not load subscribers"));
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

  // Today's cell is LIVE (the broker's own Day's P&L — Webull
  // total_day_profit_loss — or realized + open-position unrealized). While the
  // month in view contains today, quietly re-fetch every 30s (visible tabs
  // only), and also on window focus / reconnect / becoming visible, so the
  // figure stays current after the tab was backgrounded — without flashing the
  // loader or reloading the page. Only today's month polls; historical-only
  // months don't (nothing there moves).
  useEffect(() => {
    const today = iso(new Date());
    if (!(range.from <= today && today <= range.to)) return;
    const refresh = () => loadPnL(true);
    const id = setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, 30_000);
    const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
    window.addEventListener("focus", refresh);
    window.addEventListener("online", refresh);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      window.removeEventListener("focus", refresh);
      window.removeEventListener("online", refresh);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [range.from, range.to, loadPnL]);

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

  // The calendar is broker Day's P&L — sum / count / scale on that, not our
  // internal realized figure. Days the broker doesn't expose (day_pnl == null)
  // don't contribute.
  const monthTotal = data.reduce((s, d) => s + (d.day_pnl != null ? Number(d.day_pnl) : 0), 0);
  const tradingDays = data.filter(d => d.day_pnl != null).length;
  const maxAbs = Math.max(...data.map(d => (d.day_pnl != null ? Math.abs(Number(d.day_pnl)) : 0)), 1);
  const todayKey = iso(new Date());

  // Realized-only across every broker: the day's number is profit/loss from
  // trades CLOSED that day (FIFO, net of fees), matching the broker's daily
  // realized figure. Open-position unrealized mark-to-market is deliberately
  // excluded so a green realized day can't flip red just because a position
  // held overnight is currently underwater.
  const pnlTooltip = useMemo(
    () =>
      "Daily P&L — the SAME Day's P&L your connected broker (Webull / Alpaca) shows for each trading date. Today updates live from the broker through the session, then locks as history. Dates the broker doesn't expose show \"--\" rather than an estimate.",
    [],
  );

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
            <div className="text-[11px] uppercase tracking-wider flex items-center gap-1" style={{ color: "var(--muted)" }}>
              Month total
              <span
                className="inline-grid place-items-center cursor-help"
                title={pnlTooltip}
                aria-label="What does this number mean?"
              >
                <Info size={12} />
              </span>
            </div>
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
          if (!d) return <div key={i} className="h-28" />;
          const key = iso(d);
          const day = byDay[key];
          // The broker's own Day's P&L (+ %) — the ONLY figures the calendar
          // shows. null → the broker doesn't expose it for this date ("--").
          const dayPnl = day?.day_pnl != null ? Number(day.day_pnl) : null;
          const dayPct = day?.day_pnl_pct != null ? Number(day.day_pnl_pct) : null;
          const hasVal = dayPnl != null;
          const has = !!day;
          const isToday = key === todayKey;
          // Heatmap fill — green for gains / red for losses, opacity scaled to
          // the day's magnitude vs. the month's biggest move. Only for cells
          // with a broker value.
          const intensity = hasVal ? 0.12 + 0.5 * (Math.abs(dayPnl) / maxAbs) : 0;
          const bg = hasVal
            ? (dayPnl >= 0 ? `rgba(34,197,94,${intensity})` : `rgba(239,68,68,${intensity})`)
            : "var(--panel)";
          return (
            <motion.button
              key={i}
              type="button"
              onClick={has ? () => router.push(`/trades?from=${key}&to=${key}`) : undefined}
              disabled={!has}
              title={
                !has
                  ? undefined
                  : !hasVal
                    ? `No Day's P&L available for ${key}`
                    : day.live
                      ? `${day.quality === "stale" ? "Last broker value" : "Live"} ${fmtSignedUsd(dayPnl)}${dayPct != null ? ` (${dayPct >= 0 ? "+" : ""}${dayPct.toFixed(2)}%)` : ""} — from your broker on ${key}.${day.quality === "stale" ? "" : " Click to view today's trades."}`
                      : day.quality === "estimated"
                        ? `Day's P&L ${fmtSignedUsd(dayPnl)} — ESTIMATED from your trade history (no finalized broker record for ${key}).`
                        : `Day's P&L ${fmtSignedUsd(dayPnl)}${dayPct != null ? ` (${dayPct >= 0 ? "+" : ""}${dayPct.toFixed(2)}%)` : ""} — from your broker on ${key}.`
              }
              whileHover={has ? { y: -2 } : undefined}
              transition={{ duration: 0.15 }}
              className="h-28 p-2 border flex flex-col text-left"
              style={{
                borderRadius: 10,
                borderColor: isToday ? "var(--accent)" : "var(--border)",
                boxShadow: isToday ? "0 0 0 1px var(--accent)" : "none",
                background: bg,
                cursor: has ? "pointer" : "default",
              }}
            >
              {/* On a heat-filled cell the semi-transparent green/red fill
                  washes out --muted/--good/--bad text (green-on-green was the
                  worst). Use the theme's primary foreground instead: --text is
                  near-white in dark mode and dark-slate in light, so it stays
                  readable on the fill in BOTH themes (a hardcoded white would
                  break light mode). The +/- sign + fill colour still convey
                  profit vs loss. */}
              {/* On a filled cell always use --text — even for today, whose
                  --accent (teal) is invisible on the green/red fill. The accent
                  border + ring still marks today, so we don't lose that cue. */}
              <div className="flex items-center justify-between">
                <div className="text-xs font-medium" style={{ color: has ? "var(--text)" : isToday ? "var(--accent)" : "var(--muted)" }}>
                  {d.getDate()}
                </div>
                {/* Today's realized figure is still moving as trades close —
                    flag it so it reads as live, not settled. */}
                {day?.live && (day.quality === "stale" ? (
                  // The broker refresh failed — this is the last-known value, not
                  // live. Show it as stale (muted, no pulse) instead of "Live".
                  <span
                    className="inline-flex items-center gap-1 text-[9px] font-semibold uppercase tracking-wide"
                    style={{ color: "var(--muted)" }}
                    title={`Last broker value${day.last_updated_at ? ` from ${new Date(day.last_updated_at).toLocaleTimeString()}` : ""} — live refresh unavailable`}
                  >
                    <span style={{ width: 6, height: 6, borderRadius: 9999, background: "var(--muted)", display: "inline-block" }} aria-hidden />
                    Stale
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1 text-[9px] font-semibold uppercase tracking-wide" style={{ color: "var(--text)" }}>
                    <span className="animate-pulse" style={{ width: 6, height: 6, borderRadius: 9999, background: "var(--accent)", display: "inline-block" }} aria-hidden />
                    Live
                  </span>
                ))}
                {/* Settled day restored from our own trade history (no finalized
                    broker record) — flag it as an estimate, not authoritative. */}
                {has && !day?.live && day?.quality === "estimated" && (
                  <span className="text-[9px] font-semibold uppercase tracking-wide"
                    style={{ color: "var(--muted)" }} title="Estimated from trade history">est</span>
                )}
              </div>
              {has && (
                <div className="mt-auto">
                  {/* Only the broker's Day's P&L + Day's P&L % — no Real/Unreal/
                      Marked. "--" when the broker doesn't expose this date. */}
                  {hasVal ? (
                    <>
                      <div className="num font-semibold text-[15px] leading-tight" style={{ color: dayPnl > 0 ? "var(--pnl-pos)" : dayPnl < 0 ? "var(--pnl-neg)" : "var(--text-2)" }}>
                        {fmtSignedUsd(dayPnl)}
                      </div>
                      {dayPct != null && (
                        <div className="num text-[12px] leading-tight" style={{ color: dayPnl > 0 ? "var(--pnl-pos)" : dayPnl < 0 ? "var(--pnl-neg)" : "var(--text-2)" }}>
                          {`${dayPct >= 0 ? "+" : ""}${dayPct.toFixed(2)}%`}
                        </div>
                      )}
                    </>
                  ) : (
                    <div className="num text-[15px] font-semibold" style={{ color: "var(--faint)" }}>—</div>
                  )}
                </div>
              )}
            </motion.button>
          );
        })}
      </div>
      {loading && <p className="mt-3 text-sm" style={{ color: "var(--muted)" }}>Loading…</p>}
    </div>
  );
}
