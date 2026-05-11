"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { DailyPnL } from "@/lib/types";

function startOfMonth(d: Date) { return new Date(d.getFullYear(), d.getMonth(), 1); }
function endOfMonth(d: Date) { return new Date(d.getFullYear(), d.getMonth() + 1, 0); }
function iso(d: Date) { return d.toISOString().slice(0, 10); }

export default function CalendarPage() {
  const [cursor, setCursor] = useState(() => startOfMonth(new Date()));
  const [data, setData] = useState<DailyPnL[]>([]);
  const [loading, setLoading] = useState(true);

  const range = useMemo(() => ({ from: iso(startOfMonth(cursor)), to: iso(endOfMonth(cursor)) }), [cursor]);

  useEffect(() => {
    setLoading(true);
    api<DailyPnL[]>(`/api/calendar/pnl?from=${range.from}&to=${range.to}`)
      .then(setData)
      .finally(() => setLoading(false));
  }, [range.from, range.to]);

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

  return (
    <div className="space-y-4 max-w-5xl">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">P&L calendar</h1>
        <div className="flex items-center gap-2">
          <button onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1))} className="px-3 py-1 rounded border" style={{borderColor: "var(--border)"}}>‹</button>
          <div className="min-w-[10rem] text-center font-medium">
            {cursor.toLocaleString(undefined, { month: "long", year: "numeric" })}
          </div>
          <button onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1))} className="px-3 py-1 rounded border" style={{borderColor: "var(--border)"}}>›</button>
        </div>
        <div className="text-sm">
          <span style={{color: "var(--muted)"}}>Month total: </span>
          <span style={{color: monthTotal >= 0 ? "var(--good)" : "var(--bad)"}}>
            {monthTotal.toLocaleString(undefined, { style: "currency", currency: "USD" })}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-7 gap-1 text-xs" style={{color: "var(--muted)"}}>
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
            <div key={i} className="h-24 p-2 rounded border flex flex-col" style={{borderColor: "var(--border)", background: has ? (pnl >= 0 ? "rgba(34,197,94,0.08)" : "rgba(239,68,68,0.08)") : "var(--panel)"}}>
              <div className="text-xs" style={{color: "var(--muted)"}}>{d.getDate()}</div>
              {has && (
                <>
                  <div className="mt-auto font-medium" style={{color: pnl >= 0 ? "var(--good)" : "var(--bad)"}}>
                    {pnl.toLocaleString(undefined, { style: "currency", currency: "USD" })}
                  </div>
                  <div className="text-xs" style={{color: "var(--muted)"}}>{day.trade_count} trade{day.trade_count === 1 ? "" : "s"}</div>
                </>
              )}
            </div>
          );
        })}
      </div>
      {loading && <p style={{color: "var(--muted)"}}>Loading…</p>}
    </div>
  );
}
