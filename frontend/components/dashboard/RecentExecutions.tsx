"use client";

import { motion } from "framer-motion";
import { ArrowDownRight, ArrowUpRight, ChevronRight, Inbox } from "lucide-react";
import Link from "next/link";
import type { Order } from "@/lib/types";
import { fmtDateTime, fmtUsd, fmtNum, fmtSignedUsd } from "@/lib/format";

const STATUS_TONE: Record<string, { color: string; bg: string }> = {
  filled: { color: "var(--good)", bg: "var(--good-soft)" },
  partially_filled: { color: "var(--good)", bg: "var(--good-soft)" },
  accepted: { color: "var(--accent)", bg: "var(--accent-glow)" },
  submitted: { color: "var(--accent)", bg: "var(--accent-glow)" },
  pending: { color: "var(--warn)", bg: "rgba(180,120,10,0.12)" },
  retry_pending: { color: "var(--warn)", bg: "rgba(180,120,10,0.12)" },
  rejected: { color: "var(--bad)", bg: "var(--bad-soft)" },
  canceled: { color: "var(--muted)", bg: "var(--panel-2)" },
  expired: { color: "var(--muted)", bg: "var(--panel-2)" },
};

function prettyStatus(s: string) {
  return s.replace(/_/g, " ");
}

export function RecentExecutions({ orders }: { orders: Order[] }) {
  const rows = orders.slice(0, 7);

  return (
    <div className="card p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold" style={{ color: "var(--text)" }}>
          Recent executions
        </h3>
        <Link
          href="/trades"
          className="text-xs inline-flex items-center gap-0.5 no-underline focus-ring rounded"
          style={{ color: "var(--accent)" }}
        >
          Order history <ChevronRight size={13} />
        </Link>
      </div>

      {rows.length === 0 ? (
        <div
          className="flex flex-col items-center justify-center text-center py-10 gap-2"
          style={{ color: "var(--muted)" }}
        >
          <Inbox size={26} />
          <div className="text-sm">No orders yet</div>
          <div className="text-xs">Executions will appear here as they happen.</div>
        </div>
      ) : (
        <div className="space-y-1">
          {rows.map((o, i) => {
            const buy = o.side === "buy";
            const tone = STATUS_TONE[o.status] ?? STATUS_TONE.pending;
            const price = o.filled_avg_price ?? o.limit_price;
            return (
              <motion.div
                key={o.id}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.04, duration: 0.28 }}
                className="flex items-center gap-3 rounded-token px-2.5 py-2 transition-colors"
                style={{ borderBottom: "1px solid var(--border)" }}
              >
                <span
                  className="grid place-items-center rounded-full shrink-0"
                  style={{
                    width: 30,
                    height: 30,
                    background: buy ? "var(--good-soft)" : "var(--bad-soft)",
                    color: buy ? "var(--good)" : "var(--bad)",
                  }}
                >
                  {buy ? <ArrowUpRight size={15} /> : <ArrowDownRight size={15} />}
                </span>

                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="text-sm font-semibold truncate" style={{ color: "var(--text)" }}>
                      {o.symbol}
                    </span>
                    {o.instrument_type === "option" && o.option_right && (
                      <span className="chip" style={{ textTransform: "uppercase" }}>
                        {o.option_right}
                      </span>
                    )}
                  </div>
                  <div className="text-[11px]" style={{ color: "var(--muted)" }}>
                    {buy ? "Buy" : "Sell"} {fmtNum(o.quantity, 0)} @ {fmtUsd(price)}
                  </div>
                </div>

                <div className="text-right shrink-0">
                  {o.realized_pnl != null && Number(o.realized_pnl) !== 0 ? (
                    <div
                      className="text-sm font-semibold num"
                      style={{ color: Number(o.realized_pnl) >= 0 ? "var(--good)" : "var(--bad)" }}
                      title="Realized P&L on this trade"
                    >
                      {fmtSignedUsd(Number(o.realized_pnl))}
                    </div>
                  ) : (
                    <span
                      className="chip"
                      style={{ background: tone.bg, color: tone.color, borderColor: "transparent", textTransform: "capitalize" }}
                    >
                      {prettyStatus(o.status)}
                    </span>
                  )}
                  <div className="text-[10px] mt-1" style={{ color: "var(--faint)" }}>
                    {fmtDateTime(o.created_at)}
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
