"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useEventStream } from "@/lib/sse";
import { FilterBar } from "./FilterBar";
import { KpiCards } from "./KpiCards";
import { FanoutTable } from "./FanoutTable";
import { LatencyTrend } from "./LatencyTrend";
import { PlatformBrokerSplit } from "./PlatformBrokerSplit";
import { BrokerLeaderboard } from "./BrokerLeaderboard";
import { TestPanel } from "./TestPanel";
import { LoadTestHistory } from "./LoadTestHistory";
import { BrokerHealthPanel } from "./BrokerHealthPanel";
import { ListenerHealthPanel } from "./ListenerHealthPanel";
import { useDashboardData } from "./useDashboardData";
import { DEFAULT_FILTERS, type Filters, type RangeKey } from "./types";

const STORAGE_KEY = "admin-dashboard-filters";

// Read filters from the URL query (shareable links) first, then sessionStorage,
// then defaults. Client-only — runs in an effect to avoid SSR/hydration issues.
function readInitialFilters(): Filters {
  if (typeof window === "undefined") return DEFAULT_FILTERS;
  const url = new URLSearchParams(window.location.search);
  const fromUrl: Partial<Filters> = {};
  if (url.has("trader")) fromUrl.traderId = url.get("trader") || "";
  if (url.has("range")) fromUrl.range = url.get("range") as RangeKey;
  if (url.has("from")) fromUrl.from = url.get("from") || "";
  if (url.has("to")) fromUrl.to = url.get("to") || "";
  if (url.has("broker")) fromUrl.broker = url.get("broker") || "";

  let fromStore: Partial<Filters> = {};
  if (Object.keys(fromUrl).length === 0) {
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      if (raw) fromStore = JSON.parse(raw);
    } catch { /* ignore */ }
  }
  return { ...DEFAULT_FILTERS, ...fromStore, ...fromUrl };
}

function persistFilters(f: Filters) {
  if (typeof window === "undefined") return;
  try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(f)); } catch { /* ignore */ }
  const p = new URLSearchParams();
  if (f.traderId) p.set("trader", f.traderId);
  if (f.range !== "today") p.set("range", f.range);
  if (f.range === "custom") {
    if (f.from) p.set("from", f.from);
    if (f.to) p.set("to", f.to);
  }
  if (f.broker) p.set("broker", f.broker);
  const qs = p.toString();
  window.history.replaceState(null, "", qs ? `?${qs}` : window.location.pathname);
}

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <div>
        <h3 className="text-sm font-semibold uppercase tracking-widest" style={{ color: "var(--text-2)" }}>{title}</h3>
        {subtitle && <p className="text-xs" style={{ color: "var(--muted)" }}>{subtitle}</p>}
      </div>
      {children}
    </section>
  );
}

export default function AdminDashboardPage() {
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [ready, setReady] = useState(false);

  // Hydrate filters from URL/sessionStorage once on mount.
  useEffect(() => {
    setFilters(readInitialFilters());
    setReady(true);
  }, []);

  const update = useCallback((patch: Partial<Filters>) => {
    setFilters((prev) => {
      const next = { ...prev, ...patch };
      persistFilters(next);
      return next;
    });
  }, []);

  const { perf, byBroker, tests, loadTests, brokerHealth, listenerHealth, refresh } = useDashboardData(filters);

  // Live updates: any order.* event debounce-refetches all panels (one fanout
  // emits ~N child events, so debounce collapses them into a single refresh).
  const liveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEventStream((evt) => {
    if (!evt.type.startsWith("order.")) return;
    if (liveTimer.current) clearTimeout(liveTimer.current);
    liveTimer.current = setTimeout(refresh, 800);
  });

  // Overall test pass rate (across the latest result per suite) for the KPI card.
  const testPassRate = (() => {
    const rows = tests.data;
    if (!rows || rows.length === 0) return null;
    const totals = rows.reduce((a, r) => ({ passed: a.passed + r.passed, ran: a.ran + r.passed + r.failed }), { passed: 0, ran: 0 });
    return totals.ran > 0 ? totals.passed / totals.ran : null;
  })();

  // Avoid an initial fetch with default filters before hydration settles.
  if (!ready) return <div style={{ color: "var(--muted)" }}>Loading dashboard…</div>;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-xl font-bold">Performance &amp; Testing Dashboard</h2>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            Copy-trading fan-out latency, success rates, and per-broker speed — filterable by trader and time range.
          </p>
        </div>
        <button
          onClick={refresh}
          className="text-sm px-3 py-1.5 rounded-lg"
          style={{ background: "rgba(255,255,255,0.06)", border: "1px solid var(--border)", color: "var(--text-2)" }}
        >
          Refresh
        </button>
      </div>

      {/* Sticky filters */}
      <FilterBar filters={filters} onChange={update} />

      {/* KPI cards */}
      <KpiCards metrics={perf.data?.metrics ?? null} loading={perf.loading} testPassRate={testPassRate} />

      {perf.error && (
        <div className="rounded-xl p-3 text-sm" style={{ background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.3)", color: "var(--bad)" }}>
          Couldn’t load performance data: {perf.error}
        </div>
      )}

      {perf.data?.metrics.truncated && (
        <div className="rounded-xl p-2.5 text-xs" style={{ background: "rgba(250,204,21,0.08)", border: "1px solid rgba(250,204,21,0.3)", color: "#facc15" }}>
          This window has more than 2,000 fan-outs — aggregates are computed over the most recent 2,000. Narrow the range or pick a trader for exact numbers.
        </div>
      )}

      {/* Latency + platform/broker split */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2">
          <Section title="Latency trend" subtitle="Total vs platform lag per trade over the window.">
            <LatencyTrend fanouts={perf.data?.fanouts ?? null} loading={perf.loading} />
          </Section>
        </div>
        <Section title="Platform vs broker" subtitle="Who owns the time.">
          <PlatformBrokerSplit metrics={perf.data?.metrics ?? null} loading={perf.loading} />
        </Section>
      </div>

      {/* System health — is copy-trading actually working right now? */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Section title="Listener health" subtitle="Detection listeners — a down listener means that trader's trades aren't detected.">
          <ListenerHealthPanel data={listenerHealth.data} loading={listenerHealth.loading} />
        </Section>
        <Section title="Broker-connection health" subtitle="Accounts whose mirrors won't fire — disconnected, auto-pull off, or stale.">
          <BrokerHealthPanel data={brokerHealth.data} loading={brokerHealth.loading} />
        </Section>
      </div>

      {/* Broker performance */}
      <Section title="Broker performance" subtitle="Measured per-broker latency leaderboard — SnapTrade is poll-based (5–60s).">
        <BrokerLeaderboard data={byBroker.data} loading={byBroker.loading} />
      </Section>

      {/* Recent fan-outs */}
      <Section title="Recent fan-outs" subtitle="Click a row to expand the per-subscriber breakdown.">
        <FanoutTable fanouts={perf.data?.fanouts ?? null} loading={perf.loading} />
      </Section>

      {/* Testing + load-test history */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Section title="Testing" subtitle="Latest pass/fail per suite.">
          <TestPanel data={tests.data} loading={tests.loading} />
        </Section>
        <Section title="Load-test history" subtitle="Recent load-test runs.">
          <LoadTestHistory data={loadTests.data} loading={loadTests.loading} />
        </Section>
      </div>
    </div>
  );
}
