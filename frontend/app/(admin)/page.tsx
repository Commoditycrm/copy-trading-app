"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

interface Stats {
  total_users:      number;
  traders:          number;
  subscribers:      number;
  admins:           number;
  active_users:     number;
  trades_today:     number;
  fake_test_subs:   number;
}

function StatCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: number | string;
  sub?: string;
  accent?: string;
}) {
  return (
    <div
      className="rounded-xl p-5"
      style={{
        background: "linear-gradient(135deg,rgba(14,20,17,0.7) 0%,rgba(7,9,10,0.5) 100%)",
        border: "1px solid var(--border)",
      }}
    >
      <div className="text-xs uppercase tracking-widest mb-2" style={{ color: "var(--muted)" }}>
        {label}
      </div>
      <div
        className="text-3xl font-bold"
        style={{ color: accent ?? "var(--text)" }}
      >
        {value}
      </div>
      {sub && (
        <div className="text-xs mt-1" style={{ color: "var(--muted)" }}>
          {sub}
        </div>
      )}
    </div>
  );
}

export default function AdminDashboard() {
  const [stats, setStats]     = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState<string | null>(null);

  async function load() {
    try {
      const s = await api<Stats>("/api/admin/stats");
      setStats(s);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  if (loading) return <div style={{ color: "var(--muted)" }}>Loading stats…</div>;
  if (error)   return <div style={{ color: "var(--bad)" }}>Error: {error}</div>;
  if (!stats)  return null;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-bold mb-1">Platform Overview</h2>
        <p className="text-sm" style={{ color: "var(--muted)" }}>
          Live snapshot — refresh to update.{" "}
          <button onClick={load} className="underline" style={{ color: "var(--accent)" }}>
            Refresh
          </button>
        </p>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Total Users"    value={stats.total_users}   sub={`${stats.active_users} active`} />
        <StatCard label="Traders"        value={stats.traders}        accent="var(--accent)" />
        <StatCard label="Subscribers"    value={stats.subscribers}    />
        <StatCard label="Trades Today"   value={stats.trades_today}   accent="var(--good)" />
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
        <StatCard
          label="Fake Test Subscribers"
          value={stats.fake_test_subs}
          sub="Use Load Test page to seed / cleanup"
          accent={stats.fake_test_subs > 0 ? "#facc15" : undefined}
        />
        <StatCard label="Admins" value={stats.admins} sub="Platform operators" />
      </div>

      {/* Quick links */}
      <div
        className="rounded-xl p-5"
        style={{ background: "rgba(14,20,17,0.5)", border: "1px solid var(--border)" }}
      >
        <div className="text-sm font-semibold mb-3">Quick Actions</div>
        <div className="flex flex-wrap gap-3">
          <a
            href="/admin/users"
            className="text-sm px-4 py-2 rounded-lg no-underline transition-colors"
            style={{ background: "var(--accent)", color: "var(--accent-ink)", fontWeight: 600 }}
          >
            Manage Users
          </a>
          <a
            href="/admin/load-test"
            className="text-sm px-4 py-2 rounded-lg no-underline transition-colors"
            style={{ background: "rgba(250,204,21,0.15)", color: "#facc15", border: "1px solid rgba(250,204,21,0.3)", fontWeight: 600 }}
          >
            Load Test
          </a>
        </div>
      </div>
    </div>
  );
}
