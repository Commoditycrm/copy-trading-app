"use client";

import { useMemo } from "react";
import { motion } from "framer-motion";
import {
  Wallet,
  TrendingUp,
  Layers,
  Users,
  Banknote,
  AlertCircle,
} from "lucide-react";
import { useDashboard } from "@/hooks/useDashboard";
import { KpiCard, type KpiCardProps } from "@/components/dashboard/KpiCard";
import { BrokerStatusCard } from "@/components/dashboard/BrokerStatusCard";
import { RecentExecutions } from "@/components/dashboard/RecentExecutions";
import { PnlAreaChart, type PnlPoint } from "@/components/charts/PnlAreaChart";
import { DailyPnlBars, type DailyBar } from "@/components/charts/DailyPnlBars";
import { fmtUsd, fmtSignedUsd, fmtNum, fmtDate, pnlTone } from "@/lib/format";

function num(v: string | number | null | undefined): number {
  if (v === null || v === undefined || v === "") return 0;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
}

function sum<T>(rows: T[], pick: (r: T) => string | number | null | undefined): number {
  return rows.reduce((acc, r) => acc + num(pick(r)), 0);
}

function greeting(): string {
  const h = new Date().getHours();
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

function CardHeader({ title, value, tone }: { title: string; value?: string; tone?: "good" | "bad" | "flat" }) {
  const color = tone === "good" ? "var(--good)" : tone === "bad" ? "var(--bad)" : "var(--text)";
  return (
    <div className="flex items-baseline justify-between mb-3">
      <h3 className="text-sm font-semibold" style={{ color: "var(--text)" }}>{title}</h3>
      {value && <span className="num text-sm font-semibold" style={{ color }}>{value}</span>}
    </div>
  );
}

function SectionSkeleton() {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="skeleton h-[116px] rounded-card" />
        ))}
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="skeleton h-[320px] rounded-card lg:col-span-2" />
        <div className="skeleton h-[320px] rounded-card" />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="skeleton h-[260px] rounded-card" />
        <div className="skeleton h-[260px] rounded-card" />
      </div>
    </div>
  );
}

export default function DashboardPage() {
  const d = useDashboard();

  const derived = useMemo(() => {
    const totalEquity = sum(d.brokers, (b) => b.total_equity);
    const buyingPower = sum(d.brokers, (b) => b.buying_power);
    const unrealized = sum(d.positions, (p) => p.unrealized_pnl);
    const openPositions = d.positions.length;
    const realized30d = sum(d.dailyPnl, (r) => r.realized_pnl);

    // cumulative + daily series (ascending by day)
    const sorted = [...d.dailyPnl].sort((a, b) => a.day.localeCompare(b.day));
    let running = 0;
    const cumulative: PnlPoint[] = sorted.map((r) => {
      running += num(r.realized_pnl);
      return { day: r.day, value: running };
    });
    const daily: DailyBar[] = sorted.map((r) => ({ day: r.day, value: num(r.realized_pnl) }));

    const activeSubs = d.subscribers.filter((s) => s.copy_enabled).length;
    const totalSubs = d.subscribers.length;

    return { totalEquity, buyingPower, unrealized, openPositions, realized30d, cumulative, daily, activeSubs, totalSubs };
  }, [d]);

  if (d.loading) {
    return (
      <div className="max-w-[1400px] mx-auto">
        <div className="skeleton h-9 w-64 rounded-token mb-2" />
        <div className="skeleton h-4 w-40 rounded-token mb-6" />
        <SectionSkeleton />
      </div>
    );
  }

  if (d.error) {
    return (
      <div className="max-w-[1400px] mx-auto">
        <div className="card p-8 flex flex-col items-center text-center gap-3" style={{ color: "var(--muted)" }}>
          <AlertCircle size={28} style={{ color: "var(--bad)" }} />
          <div className="text-sm" style={{ color: "var(--text)" }}>{d.error}</div>
          <button onClick={() => location.reload()} className="btn-ghost px-4 py-2 text-sm">
            Retry
          </button>
        </div>
      </div>
    );
  }

  const isTrader = d.user?.role === "trader";
  const name = d.user?.display_name || d.user?.email?.split("@")[0] || "there";

  const kpis: KpiCardProps[] = [
    {
      label: "Total equity",
      value: derived.totalEquity,
      format: fmtUsd,
      icon: Wallet,
      tone: "accent",
      sub: `Across ${d.brokers.length} broker${d.brokers.length === 1 ? "" : "s"}`,
    },
    {
      label: "Realized P&L · 30d",
      value: derived.realized30d,
      format: fmtSignedUsd,
      icon: TrendingUp,
      tone: pnlTone(derived.realized30d) === "good" ? "good" : pnlTone(derived.realized30d) === "bad" ? "bad" : "neutral",
      sub: "Last 30 days",
    },
    {
      label: "Open positions",
      value: derived.openPositions,
      format: (n) => fmtNum(n, 0),
      icon: Layers,
      tone: "neutral",
      sub: `Unrealized ${fmtSignedUsd(derived.unrealized)}`,
      delta:
        derived.openPositions > 0
          ? { text: fmtSignedUsd(derived.unrealized), tone: pnlTone(derived.unrealized) }
          : null,
    },
    isTrader
      ? {
          label: "Active subscribers",
          value: derived.activeSubs,
          format: (n) => fmtNum(n, 0),
          icon: Users,
          tone: "accent",
          sub: `${derived.activeSubs}/${derived.totalSubs} copying`,
        }
      : {
          label: "Buying power",
          value: derived.buyingPower,
          format: fmtUsd,
          icon: Banknote,
          tone: "neutral",
          sub: d.subSettings ? `Multiplier ${fmtNum(d.subSettings.multiplier, 2)}×` : "Available to trade",
        },
  ];

  return (
    <div className="max-w-[1400px] mx-auto">
      {/* Greeting */}
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        className="mb-6"
      >
        <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight" style={{ color: "var(--text)" }}>
          {greeting()}, {name}
        </h1>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          {fmtDate(new Date())} · {isTrader ? "Trader" : "Subscriber"} overview
        </p>
      </motion.div>

      {/* KPI row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {kpis.map((k, i) => (
          <KpiCard key={k.label} {...k} index={i} />
        ))}
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mt-4">
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, delay: 0.1 }}
          className="card p-5 lg:col-span-2"
        >
          <CardHeader
            title="Cumulative realized P&L · 30d"
            value={fmtSignedUsd(derived.realized30d)}
            tone={pnlTone(derived.realized30d)}
          />
          <div className="h-[260px]">
            {derived.cumulative.length > 1 ? (
              <PnlAreaChart data={derived.cumulative} />
            ) : (
              <EmptyChart label="Not enough P&L history yet" />
            )}
          </div>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, delay: 0.16 }}
          className="card p-5"
        >
          <CardHeader title="Daily P&L" />
          <div className="h-[260px]">
            {derived.daily.some((x) => x.value !== 0) ? (
              <DailyPnlBars data={derived.daily} />
            ) : (
              <EmptyChart label="No realized P&L in this window" />
            )}
          </div>
        </motion.div>
      </div>

      {/* Status row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.45, delay: 0.22 }}>
          <BrokerStatusCard brokers={d.brokers} />
        </motion.div>
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.45, delay: 0.28 }}>
          <RecentExecutions orders={d.orders} />
        </motion.div>
      </div>
    </div>
  );
}

function EmptyChart({ label }: { label: string }) {
  return (
    <div className="h-full grid place-items-center text-center text-sm" style={{ color: "var(--muted)" }}>
      {label}
    </div>
  );
}
