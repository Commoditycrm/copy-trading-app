"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";

interface Stats {
  total_users:      number;
  traders:          number;
  subscribers:      number;
  admins:           number;
  active_users:     number;
  trades_today:     number;
  fake_test_subs:   number;
}

interface FanoutThreshold {
  default:   number;
  override:  number | null;
  effective: number;
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
        background: "var(--panel)",
        border: "1px solid var(--border)",
        boxShadow: "var(--shadow-card)",
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

      {/* Fanout performance tuning */}
      <FanoutThresholdCard />

      {/* Quick links */}
      <div
        className="rounded-xl p-5"
        style={{ background: "var(--panel)", border: "1px solid var(--border)" }}
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

/** Runtime knob for the copy_engine hybrid threshold.
 *
 *  - Below the threshold, fanout runs the per-iteration code path
 *    (lower first-sub pick_lag, scales linearly with N).
 *  - At/above the threshold, it switches to the batched code path
 *    (higher floor, flat scaling).
 *
 *  Stored in Redis on top of the env default — admin can change at
 *  runtime without a restart, and "Reset" wipes the override so the
 *  env-default value takes effect again. */
function FanoutThresholdCard() {
  const [state, setState] = useState<FanoutThreshold | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy]   = useState(false);
  const [loading, setLoading] = useState(true);

  async function loadConfig() {
    try {
      const s = await api<FanoutThreshold>("/api/admin/config/fanout-batch-threshold");
      setState(s);
      setInput(String(s.effective));
    } catch (e) {
      notify.fromError(e, "Could not load fanout threshold");
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { loadConfig(); }, []);

  async function save() {
    const n = Number(input);
    if (!Number.isFinite(n) || n < 1 || n > 10000) {
      notify.warn("Threshold must be between 1 and 10,000.");
      return;
    }
    setBusy(true);
    try {
      const s = await api<FanoutThreshold>("/api/admin/config/fanout-batch-threshold", {
        method: "PATCH", body: JSON.stringify({ threshold: Math.floor(n) }),
      });
      setState(s);
      setInput(String(s.effective));
      notify.success(`Threshold set to ${s.effective}`);
    } catch (e) {
      notify.fromError(e, "Could not update threshold");
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    try {
      const s = await api<FanoutThreshold>("/api/admin/config/fanout-batch-threshold", {
        method: "PATCH", body: JSON.stringify({ threshold: null }),
      });
      setState(s);
      setInput(String(s.effective));
      notify.success("Reset to env default");
    } catch (e) {
      notify.fromError(e, "Could not reset threshold");
    } finally {
      setBusy(false);
    }
  }

  const overriding = state?.override !== null && state?.override !== undefined;

  return (
    <div
      className="rounded-xl p-5"
      style={{ background: "var(--panel)", border: "1px solid var(--border)" }}
    >
      <div className="flex items-start justify-between gap-4 mb-3 flex-wrap">
        <div>
          <div className="text-sm font-semibold mb-1">Fanout Performance</div>
          <div className="text-xs leading-relaxed" style={{ color: "var(--muted)" }}>
            Subscriber count above which <code>copy_engine</code> switches from
            the per-iteration path (low floor, linear) to the batched path
            (higher floor, flat). Defaults to <strong>75</strong>; raise it
            for traders with smaller subscriber bases, lower it for heavily-
            followed traders.
          </div>
        </div>
        {state && (
          <div className="flex items-center gap-2 text-xs whitespace-nowrap">
            <span style={{ color: "var(--muted)" }}>Effective:</span>
            <strong className="tabular-nums" style={{ color: "var(--accent)" }}>
              {loading ? "—" : state.effective}
            </strong>
            <span
              className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider"
              style={{
                background: overriding ? "rgba(245,158,11,0.15)" : "rgba(34,197,94,0.12)",
                color: overriding ? "#f59e0b" : "var(--good)",
                border: `1px solid ${overriding ? "rgba(245,158,11,0.35)" : "rgba(34,197,94,0.25)"}`,
              }}
            >
              {overriding ? `Override (default ${state.default})` : "Default"}
            </span>
          </div>
        )}
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <label className="text-xs" style={{ color: "var(--muted)" }}>
          Threshold
        </label>
        <input
          type="number"
          min={1}
          max={10000}
          step={1}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={loading || busy}
          className="w-28 px-3 py-1.5 text-sm rounded-lg bg-transparent border tabular-nums focus:outline-none focus:border-[var(--accent)]"
          style={{ borderColor: "var(--border)" }}
        />
        <button
          onClick={save}
          disabled={busy || loading || (state ? input === String(state.effective) : true)}
          className="px-3 py-1.5 text-xs rounded-lg font-semibold inline-flex items-center gap-1.5 disabled:opacity-30 disabled:cursor-not-allowed"
          style={{ background: "var(--accent)", color: "var(--accent-ink)" }}
        >
          {busy && <Spinner />}
          Save
        </button>
        {overriding && (
          <button
            onClick={reset}
            disabled={busy || loading}
            className="px-3 py-1.5 text-xs rounded-lg border disabled:opacity-40"
            style={{ borderColor: "var(--border)", color: "var(--muted)" }}
            title="Clear override; effective value reverts to env default"
          >
            Reset to default
          </button>
        )}
      </div>
    </div>
  );
}
