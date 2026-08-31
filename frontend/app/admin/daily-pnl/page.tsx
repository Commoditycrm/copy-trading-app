"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { fmtSignedUsd } from "@/lib/format";
import { SearchableSelect, type SelectOption } from "@/components/SearchableSelect";

interface DailyRow {
  day: string;
  trader_pnl: string;
  subscriber_pnl: string;
  total: string;
  users: number;
}

// Shape of the per-user endpoint (DailyPnL) — the SAME figures the user's Calendar shows.
interface CalRow {
  day: string;
  realized_pnl: string;   // the shown (marked) number
  trade_count: number;
  unrealized_pnl: string | null;
  live: boolean;
}

interface UserRow { id: string; email: string; role: string; display_name: string | null; }

const todayISO = () => new Date().toISOString().slice(0, 10);
function daysAgoISO(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

function Money({ v }: { v: string }) {
  const n = Number(v);
  return (
    <span className="num font-medium" style={{ color: n > 0 ? "var(--good)" : n < 0 ? "var(--bad)" : "var(--text-2)" }}>
      {fmtSignedUsd(n)}
    </span>
  );
}

export default function AdminDailyPnlPage() {
  const [rows, setRows] = useState<DailyRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [role, setRole] = useState("");                // "" | "trader" | "subscriber"
  const [userId, setUserId] = useState("");            // "" = whole role / all
  const [users, setUsers] = useState<UserRow[]>([]);

  async function load() {
    setLoading(true);
    try {
      if (userId) {
        // ONE user → use the per-user endpoint that reuses the user's own
        // Calendar computation, so the figures match exactly what they see.
        // That endpoint requires a date range; default to the last 90 days.
        const f = from || daysAgoISO(90);
        const t = to || todayISO();
        const q = new URLSearchParams({ from: f, to: t });
        const data = await api<CalRow[]>(`/api/admin/users/${userId}/pnl-calendar?${q.toString()}`);
        const urole = users.find((u) => u.id === userId)?.role ?? "trader";
        const mapped: DailyRow[] = data
          .filter((c) => Number(c.realized_pnl) !== 0 || c.trade_count > 0)
          .map((c) => ({
            day: c.day,
            trader_pnl: urole === "trader" ? c.realized_pnl : "0",
            subscriber_pnl: urole === "subscriber" ? c.realized_pnl : "0",
            total: c.realized_pnl,
            users: 1,
          }))
          .sort((a, b) => (a.day < b.day ? 1 : -1)); // newest first
        setRows(mapped);
      } else {
        // No specific user → the all-users aggregate (unchanged).
        const q = new URLSearchParams();
        if (from) q.set("from", from);
        if (to) q.set("to", to);
        if (role) q.set("role", role);
        const qs = q.toString();
        setRows(await api<DailyRow[]>(`/api/admin/daily-pnl${qs ? `?${qs}` : ""}`));
      }
    } catch (e) {
      notify.fromError(e, "Could not load daily P&L");
    } finally {
      setLoading(false);
    }
  }
  // Auto-apply: reload on mount and whenever ANY filter (user / from / to)
  // changes — no Apply click needed.
  useEffect(() => { load(); }, [role, userId, from, to]); // eslint-disable-line react-hooks/exhaustive-deps
  // Users for the picker — traders + subscribers (admins excluded). The users
  // endpoint caps limit at 200, so fetch traders and subscribers separately
  // (each well under the cap) and merge, rather than one over-limit call that
  // 422s and leaves the dropdown empty.
  useEffect(() => {
    Promise.all([
      api<{ items: UserRow[] }>("/api/admin/users?role=trader&limit=200").catch(() => ({ items: [] as UserRow[] })),
      api<{ items: UserRow[] }>("/api/admin/users?role=subscriber&limit=200").catch(() => ({ items: [] as UserRow[] })),
    ]).then(([t, s]) => setUsers([...t.items, ...s.items]));
  }, []);

  // Name options for the CURRENT role (the Role dropdown narrows the pool).
  // First entry = no specific person → the whole role / all.
  const userOptions: SelectOption[] = useMemo(() => {
    const pool = role ? users.filter((u) => u.role === role) : users;
    const first = role === "trader" ? "All traders" : role === "subscriber" ? "All subscribers" : "Anyone";
    return [{ value: "", label: first }, ...pool.map((u) => ({ value: u.id, label: u.display_name || u.email }))];
  }, [users, role]);

  const grand = rows.reduce(
    (a, r) => ({ t: a.t + Number(r.trader_pnl), s: a.s + Number(r.subscriber_pnl), all: a.all + Number(r.total) }),
    { t: 0, s: 0, all: 0 },
  );

  const th = "px-4 py-3 text-xs font-semibold whitespace-nowrap";
  const td = "px-4 py-2.5 text-sm whitespace-nowrap";

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-xl font-bold">Daily P&L{userId ? " — Single User" : " — All Users"}</h2>
          {userId ? (
            <p className="text-sm mt-1" style={{ color: "var(--good)" }}>
              Showing this user&apos;s own Calendar figures — matches exactly what the user sees.
              (Last 90 days by default; set a date range to change.)
            </p>
          ) : (
            <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
              Per-day total from the broker-direct snapshots, split trader vs subscriber. ET days.
              Webull is realized, Alpaca is marked — each broker&apos;s own daily figure.
            </p>
          )}
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {/* Role dropdown — picks the group; resets any specific-name pick. */}
          <select
            value={role}
            onChange={(e) => { setRole(e.target.value); setUserId(""); }}
            className="text-sm px-2.5 py-1.5 rounded-lg"
            style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}
            aria-label="Role"
            title="All / Traders / Subscribers"
          >
            <option value="">All users</option>
            <option value="trader">Traders</option>
            <option value="subscriber">Subscribers</option>
          </select>
          {/* Name box — searchable, scoped to the chosen role. */}
          <SearchableSelect
            value={userId}
            options={userOptions}
            onChange={setUserId}
            placeholder="Search name…"
            className="w-[200px]"
            style={{ height: 34 }}
          />
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)}
            className="text-sm px-2.5 py-1.5 rounded-lg" style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }} aria-label="From date" title="From (blank = all time)" />
          <span className="text-xs" style={{ color: "var(--muted)" }}>–</span>
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)}
            className="text-sm px-2.5 py-1.5 rounded-lg" style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }} aria-label="To date" title="To (blank = all time)" />
          {(role || userId || from || to) && (
            <button
              onClick={() => { setRole(""); setUserId(""); setFrom(""); setTo(""); }}
              className="text-sm px-3 py-1.5 rounded-lg"
              style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}
              title="Reset to all users, all time"
            >
              Clear
            </button>
          )}
        </div>
      </div>

      <div className="rounded-xl overflow-hidden" style={{ border: "1px solid var(--border)" }}>
        <div className="overflow-auto" style={{ maxHeight: "72vh" }}>
          <table className="w-full">
            <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
              <tr style={{ borderBottom: "1px solid var(--border)" }}>
                <th className={`${th} text-left`} style={{ color: "var(--muted)" }}>Date (ET)</th>
                <th className={`${th} text-right`} style={{ color: "var(--muted)" }}>Trader P&L</th>
                <th className={`${th} text-right`} style={{ color: "var(--muted)" }}>Subscriber P&L</th>
                <th className={`${th} text-right`} style={{ color: "var(--muted)" }}>Total P&L</th>
                <th className={`${th} text-right`} style={{ color: "var(--muted)" }}>Users</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={5} className="px-4 py-10 text-center" style={{ color: "var(--muted)" }}>Loading…</td></tr>
              ) : rows.length === 0 ? (
                <tr><td colSpan={5} className="px-4 py-10 text-center" style={{ color: "var(--muted)" }}>
                  No P&L data for this selection.{(from || to) ? " The date range may be empty (future/no-trade days) — clear the dates for all time." : ""}
                </td></tr>
              ) : (
                <>
                  <tr style={{ background: "var(--panel-2)", borderBottom: "1px solid var(--border)" }}>
                    <td className={`${td} font-semibold`}>Total ({rows.length} days)</td>
                    <td className={`${td} text-right`}><Money v={String(grand.t)} /></td>
                    <td className={`${td} text-right`}><Money v={String(grand.s)} /></td>
                    <td className={`${td} text-right`}><Money v={String(grand.all)} /></td>
                    <td className={`${td} text-right`} style={{ color: "var(--muted)" }}>—</td>
                  </tr>
                  {rows.map((r) => (
                    <tr key={r.day} style={{ borderBottom: "1px solid var(--border)" }}>
                      <td className={`${td} num`}>{r.day}</td>
                      <td className={`${td} text-right`}><Money v={r.trader_pnl} /></td>
                      <td className={`${td} text-right`}><Money v={r.subscriber_pnl} /></td>
                      <td className={`${td} text-right`}><Money v={r.total} /></td>
                      <td className={`${td} text-right num`} style={{ color: "var(--text-2)" }}>{r.users}</td>
                    </tr>
                  ))}
                </>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
