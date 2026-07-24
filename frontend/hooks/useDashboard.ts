"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { getSnapshot, setSnapshot } from "@/lib/swrCache";
import type {
  BrokerAccount,
  DailyPnL,
  Order,
  Page,
  Position,
  SubscriberSettings,
  SubscriberSummary,
  User,
} from "@/lib/types";

export interface DashboardData {
  user: User | null;
  positions: Position[];
  orders: Order[];
  brokers: BrokerAccount[];
  dailyPnl: DailyPnL[];
  subscribers: SubscriberSummary[]; // trader only
  subSettings: SubscriberSettings | null; // subscriber only
  loading: boolean;
  error: string | null;
}

const EMPTY: DashboardData = {
  user: null,
  positions: [],
  orders: [],
  brokers: [],
  dailyPnl: [],
  subscribers: [],
  subSettings: null,
  loading: true,
  error: null,
};

function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/**
 * Fetches everything the dashboard needs from EXISTING endpoints only — no new
 * APIs. Role-aware: traders also pull their subscriber roster; subscribers pull
 * their copy settings. All sub-fetches degrade gracefully (a failing broker or
 * P&L call doesn't blank the whole page).
 */
const DASH_KEY = "dashboard";

export function useDashboard(): DashboardData {
  // Stale-while-revalidate: paint the last snapshot instantly on return
  // navigation (loading:false), then revalidate below. Cleared on logout.
  const [data, setData] = useState<DashboardData>(() => {
    const snap = getSnapshot<DashboardData>(DASH_KEY);
    return snap ? { ...snap, loading: false, error: null } : EMPTY;
  });

  useEffect(() => {
    let cancelled = false;
    const hadSnapshot = getSnapshot<DashboardData>(DASH_KEY) !== undefined;

    (async () => {
      try {
        const user = await api<User>("/api/auth/me");
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        const to = new Date();
        const from = new Date();
        from.setDate(from.getDate() - 29);
        const fromS = isoDay(from);
        const toS = isoDay(to);

        const [positions, orders, brokers, dailyPnl] = await Promise.all([
          api<Position[]>("/api/positions").catch(() => [] as Position[]),
          api<Order[]>("/api/trades?limit=200").catch(() => [] as Order[]),
          api<BrokerAccount[]>("/api/brokers").catch(() => [] as BrokerAccount[]),
          api<DailyPnL[]>(
            `/api/calendar/pnl?from=${fromS}&to=${toS}&tz=${encodeURIComponent(tz)}`
          ).catch(() => [] as DailyPnL[]),
        ]);

        let subscribers: SubscriberSummary[] = [];
        let subSettings: SubscriberSettings | null = null;
        if (user.role === "trader") {
          // /api/subscribers is paginated now — unwrap .items. High limit so the
          // active/total counts cover the whole roster, not just one page.
          subscribers = await api<Page<SubscriberSummary>>("/api/subscribers?limit=1000")
            .then((p) => p.items)
            .catch(() => [] as SubscriberSummary[]);
        } else if (user.role === "subscriber") {
          subSettings = await api<SubscriberSettings>("/api/settings/subscriber").catch(
            () => null
          );
        }

        if (cancelled) return;
        const fresh: DashboardData = {
          user,
          positions,
          orders,
          brokers,
          dailyPnl,
          subscribers,
          subSettings,
          loading: false,
          error: null,
        };
        setSnapshot(DASH_KEY, fresh);
        setData(fresh);
      } catch {
        if (!cancelled) {
          // Keep showing the last good snapshot on a transient failure instead
          // of blanking to an error card; only surface the error on a cold load.
          setData((d) => hadSnapshot
            ? { ...d, loading: false }
            : { ...d, loading: false, error: "Could not load dashboard data." });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  return data;
}
