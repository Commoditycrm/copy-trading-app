"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { SearchableSelect, type SelectOption } from "@/components/SearchableSelect";
import type { Filters, RangeKey, Trader } from "./types";

const RANGES: { key: RangeKey; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "7d", label: "7d" },
  { key: "30d", label: "30d" },
  { key: "custom", label: "Custom" },
];

// Broker filter for the latency panels (enum value matches broker_accounts.broker).
// Lets you isolate Alpaca so the slow SnapTrade poll doesn't skew the view (§5).
const BROKER_OPTIONS: SelectOption[] = [
  { value: "", label: "All brokers" },
  { value: "alpaca", label: "Alpaca" },
  { value: "snaptrade", label: "SnapTrade" },
  { value: "ibkr", label: "IBKR" },
];

interface Props {
  filters: Filters;
  onChange: (patch: Partial<Filters>) => void;
}

/** Sticky filter bar — trader + time range drive every panel. */
export function FilterBar({ filters, onChange }: Props) {
  const [traders, setTraders] = useState<Trader[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api<Trader[]>("/api/settings/traders")
      .then((t) => { if (!cancelled) setTraders(t); })
      .catch(() => { /* non-fatal — dropdown just shows "All traders" */ })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const traderOptions: SelectOption[] = [
    { value: "", label: "All traders" },
    ...traders.map((t) => ({
      value: t.id,
      label: t.business_name || t.display_name || t.email,
    })),
  ];

  return (
    <div
      className="sticky top-0 z-10 flex flex-wrap items-center gap-3 px-4 py-3 rounded-xl"
      style={{ background: "var(--panel)", border: "1px solid var(--border)", backdropFilter: "blur(6px)" }}
    >
      {/* Trader */}
      <div className="flex items-center gap-2">
        <span className="text-xs uppercase tracking-widest" style={{ color: "var(--muted)" }}>Trader</span>
        <div style={{ minWidth: 220 }}>
          <SearchableSelect
            value={filters.traderId}
            options={traderOptions}
            loading={loading}
            placeholder="All traders"
            onChange={(v) => onChange({ traderId: v })}
            style={{ height: 36 }}
          />
        </div>
      </div>

      {/* Range segmented control */}
      <div className="flex items-center gap-2">
        <span className="text-xs uppercase tracking-widest" style={{ color: "var(--muted)" }}>Range</span>
        <div className="flex p-0.5 rounded-lg" style={{ background: "var(--bg-tint)", border: "1px solid var(--border)" }}>
          {RANGES.map((r) => {
            const active = filters.range === r.key;
            return (
              <button
                key={r.key}
                onClick={() => onChange({ range: r.key })}
                className="text-sm px-3 py-1.5 rounded-md transition-colors"
                style={{
                  background: active ? "var(--accent, #3b82f6)" : "transparent",
                  color: active ? "#fff" : "var(--text-2)",
                  fontWeight: active ? 600 : 400,
                }}
              >
                {r.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Custom date inputs */}
      {filters.range === "custom" && (
        <div className="flex items-center gap-2">
          <input
            type="datetime-local"
            value={filters.from}
            onChange={(e) => onChange({ from: e.target.value })}
            className="text-sm px-2 py-1.5 rounded-lg"
            style={{ background: "var(--bg-tint)", border: "1px solid var(--border)", color: "var(--text)" }}
          />
          <span style={{ color: "var(--muted)" }}>→</span>
          <input
            type="datetime-local"
            value={filters.to}
            onChange={(e) => onChange({ to: e.target.value })}
            className="text-sm px-2 py-1.5 rounded-lg"
            style={{ background: "var(--bg-tint)", border: "1px solid var(--border)", color: "var(--text)" }}
          />
        </div>
      )}

      {/* Broker (scopes the latency panels) */}
      <div className="flex items-center gap-2">
        <span className="text-xs uppercase tracking-widest" style={{ color: "var(--muted)" }}>Broker</span>
        <div style={{ minWidth: 150 }}>
          <SearchableSelect
            value={filters.broker}
            options={BROKER_OPTIONS}
            searchable={false}
            placeholder="All brokers"
            onChange={(v) => onChange({ broker: v })}
            style={{ height: 36 }}
          />
        </div>
      </div>
    </div>
  );
}
