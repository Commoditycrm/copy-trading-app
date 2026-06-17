"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type {
  BrokerAccount,
  DailyPnL,
  Order,
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
export function useDashboard(): DashboardData {
  const [data, setData] = useState<DashboardData>(EMPTY);

  useEffect(() => {
    let cancelled = false;

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
          subscribers = await api<SubscriberSummary[]>("/api/subscribers").catch(
            () => [] as SubscriberSummary[]
          );
        } else if (user.role === "subscriber") {
          subSettings = await api<SubscriberSettings>("/api/settings/subscriber").catch(
            () => null
          );
        }

        if (cancelled) return;
        setData({
          user,
          positions,
          orders,
          brokers,
          dailyPnl,
          subscribers,
          subSettings,
          loading: false,
          error: null,
        });
      } catch {
        if (!cancelled) {
          setData((d) => ({ ...d, loading: false, error: "Could not load dashboard data." }));
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  return data;
}
