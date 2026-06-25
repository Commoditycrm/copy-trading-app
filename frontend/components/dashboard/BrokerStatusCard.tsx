"use client";

import { motion } from "framer-motion";
import { Plug, PlugZap, AlertTriangle, ChevronRight } from "lucide-react";
import Link from "next/link";
import type { BrokerAccount } from "@/lib/types";
import { fmtUsd, fmtDateTime } from "@/lib/format";

const STATUS_META = {
  connected: { label: "Connected", color: "var(--good)", bg: "var(--good-soft)", Icon: PlugZap },
  pending: { label: "Pending", color: "var(--warn)", bg: "rgba(180,120,10,0.12)", Icon: Plug },
  error: { label: "Error", color: "var(--bad)", bg: "var(--bad-soft)", Icon: AlertTriangle },
} as const;

function brokerLabel(b: BrokerAccount): string {
  if (b.broker === "snaptrade" && b.brokerage_name) return b.brokerage_name;
  return b.broker.charAt(0).toUpperCase() + b.broker.slice(1);
}

export function BrokerStatusCard({ brokers }: { brokers: BrokerAccount[] }) {
  return (
    <div className="card p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold" style={{ color: "var(--text)" }}>
          Broker connections
        </h3>
        <Link
          href="/brokers"
          className="text-xs inline-flex items-center gap-0.5 no-underline focus-ring rounded"
          style={{ color: "var(--accent)" }}
        >
          Manage <ChevronRight size={13} />
        </Link>
      </div>

      {brokers.length === 0 ? (
        <Link
          href="/brokers"
          prefetch
          className="flex items-center gap-3 rounded-token p-4 no-underline hover-lift"
          style={{ border: "1px dashed var(--border-strong)", color: "var(--text-2)" }}
        >
          <span
            className="grid place-items-center rounded-token shrink-0"
            style={{ width: 38, height: 38, background: "var(--chip-bg)", border: "1px solid var(--border)", color: "var(--accent)" }}
          >
            <Plug size={18} />
          </span>
          <div className="min-w-0">
            <div className="text-sm font-medium" style={{ color: "var(--text)" }}>
              No broker connected
            </div>
            <div className="text-xs" style={{ color: "var(--muted)" }}>
              Connect a brokerage to start trading
            </div>
          </div>
        </Link>
      ) : (
        <div className="space-y-2.5">
          {brokers.map((b, i) => {
            const meta = STATUS_META[b.connection_status] ?? STATUS_META.pending;
            return (
              <motion.div
                key={b.id}
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: i * 0.05, duration: 0.3 }}
                className="flex items-center gap-3 rounded-token p-3"
                style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}
              >
                <span
                  className="grid place-items-center rounded-token shrink-0"
                  style={{ width: 36, height: 36, background: meta.bg, color: meta.color }}
                >
                  <meta.Icon size={17} />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium truncate" style={{ color: "var(--text)" }}>
                      {brokerLabel(b)}
                    </span>
                    <span
                      className="chip"
                      style={{ background: "var(--panel)", color: "var(--muted)" }}
                    >
                      {b.is_paper ? "Paper" : "Live"}
                    </span>
                  </div>
                  <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                    {b.balance_updated_at
                      ? `Updated ${fmtDateTime(b.balance_updated_at)}`
                      : "Awaiting balance sync"}
                  </div>
                </div>
                <div className="text-right shrink-0">
                  <div className="num text-sm font-semibold" style={{ color: "var(--text)" }}>
                    {fmtUsd(b.total_equity)}
                  </div>
                  <div className="text-[11px]" style={{ color: meta.color }}>
                    {meta.label}
                  </div>
                </div>
              </motion.div>
            );
          })}
        </div>
      )}
    </div>
  );
}
