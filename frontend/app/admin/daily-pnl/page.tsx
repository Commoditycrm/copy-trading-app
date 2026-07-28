"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { fmtSignedUsd } from "@/lib/format";

interface DailyRow {
  day: string;
  trader_pnl: string;
  subscriber_pnl: string;
  total: string;
  users: number;
}

interface UserRow { id: string; email: string; role: string; display_name: string | null; }

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
  const [userId, setUserId] = useState("");            // "" = all users
  const [users, setUsers] = useState<UserRow[]>([]);

  async function load() {
    setLoading(true);
    try {
      const q = new URLSearchParams();
      if (from) q.set("from", from);
      if (to) q.set("to", to);
      if (userId) q.set("user_id", userId);
      const qs = q.toString();
      setRows(await api<DailyRow[]>(`/api/admin/daily-pnl${qs ? `?${qs}` : ""}`));
    } catch (e) {
      notify.fromError(e, "Could not load daily P&L");
    } finally {
      setLoading(false);
    }
  }
  // Auto-apply: reload on mount and whenever ANY filter (user / from / to)
  // changes — no Apply click needed.
  useEffect(() => { load(); }, [userId, from, to]); // eslint-disable-line react-hooks/exhaustive-deps
  // Users for the picker — traders + subscribers (admins excluded), by role.
  useEffect(() => {
    api<{ items: UserRow[] }>("/api/admin/users?limit=1000")
      .then((p) => setUsers(p.items.filter((u) => u.role !== "admin")))
      .catch(() => {});
  }, []);

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
          <h2 className="text-xl font-bold">Daily P&L — All Users</h2>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            Per-day total from the broker-direct snapshots, split trader vs subscriber. ET days.
            Webull is realized, Alpaca is marked — each broker's own daily figure.
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <select
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            className="text-sm px-2.5 py-1.5 rounded-lg max-w-[220px]"
            style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}
            aria-label="Filter by user"
            title="Show one trader/subscriber, or all"
          >
            <option value="">All users</option>
            <optgroup label="Traders">
              {users.filter((u) => u.role === "trader").map((u) => (
                <option key={u.id} value={u.id}>{u.display_name || u.email}</option>
              ))}
            </optgroup>
            <optgroup label="Subscribers">
              {users.filter((u) => u.role === "subscriber").map((u) => (
                <option key={u.id} value={u.id}>{u.display_name || u.email}</option>
              ))}
            </optgroup>
          </select>
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)}
            className="text-sm px-2.5 py-1.5 rounded-lg" style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }} aria-label="From date" title="From (blank = all time)" />
          <span className="text-xs" style={{ color: "var(--muted)" }}>–</span>
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)}
            className="text-sm px-2.5 py-1.5 rounded-lg" style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }} aria-label="To date" title="To (blank = all time)" />
          {(userId || from || to) && (
            <button
              onClick={() => { setUserId(""); setFrom(""); setTo(""); }}
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
