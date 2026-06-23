"use client";

import { useEffect, useRef, useState } from "react";
import type { DependencyList } from "react";
import { api } from "@/lib/api";
import {
  buildQuery,
  type BrokerHealth,
  type BrokerStat,
  type Filters,
  type ListenerHealth,
  type LoadTestRun,
  type PerfData,
  type TestSuiteResult,
} from "./types";

export interface Resource<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

/** Generic debounced fetch. `pathFn` is evaluated inside the effect so a moving
 *  window (`new Date()`) is snapshotted per fetch, while `deps` stay stable —
 *  otherwise the query string would change every render and refetch forever. */
function useResource<T>(pathFn: () => string, deps: DependencyList, debounceMs = 250): Resource<T> {
  const [state, setState] = useState<Resource<T>>({ data: null, loading: true, error: null });

  useEffect(() => {
    let cancelled = false;
    setState((s) => ({ ...s, loading: true, error: null }));
    const t = setTimeout(() => {
      api<T>(pathFn())
        .then((d) => { if (!cancelled) setState({ data: d, loading: false, error: null }); })
        .catch((e) => { if (!cancelled) setState({ data: null, loading: false, error: e?.message ?? "Failed to load" }); });
    }, debounceMs);
    return () => { cancelled = true; clearTimeout(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}

export interface DashboardData {
  perf: Resource<PerfData>;
  byBroker: Resource<BrokerStat[]>;
  tests: Resource<TestSuiteResult[]>;
  loadTests: Resource<LoadTestRun[]>;
  brokerHealth: Resource<BrokerHealth>;
  listenerHealth: Resource<ListenerHealth>;
  refresh: () => void;
}

/** Fetches every panel's data from the filtered admin endpoints. Each resource
 *  has its own loading/error so one slow call never blocks the page. */
export function useDashboardData(filters: Filters): DashboardData {
  const [refreshKey, setRefreshKey] = useState(0);
  // Stable dependency: only refetch when the filters or an explicit refresh change.
  const key = JSON.stringify(filters);

  const perf = useResource<PerfData>(
    () => `/api/admin/performance/fanouts?${buildQuery(filters, new Date(), { broker: true })}&limit=50`,
    [key, refreshKey],
  );
  const byBroker = useResource<BrokerStat[]>(
    () => `/api/admin/performance/by-broker?${buildQuery(filters, new Date())}`,
    [key, refreshKey],
  );

  // Testing + load-test history are global (not trader/range scoped) — refetch
  // only on an explicit refresh.
  const tests = useResource<TestSuiteResult[]>(() => "/api/admin/tests/latest", [refreshKey]);
  const loadTests = useResource<LoadTestRun[]>(() => "/api/admin/load-test/history?limit=20", [refreshKey]);

  // System health — also global; refetch on refresh / SSE.
  const brokerHealth = useResource<BrokerHealth>(() => "/api/admin/broker-health", [refreshKey]);
  const listenerHealth = useResource<ListenerHealth>(() => "/api/admin/listener-health", [refreshKey]);

  // Keep a ref so SSE handlers (M5) can trigger a refresh without re-subscribing.
  const refreshRef = useRef(() => setRefreshKey((k) => k + 1));
  return { perf, byBroker, tests, loadTests, brokerHealth, listenerHealth, refresh: refreshRef.current };
}
