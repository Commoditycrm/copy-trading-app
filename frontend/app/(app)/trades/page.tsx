"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useEventStream } from "@/lib/sse";
import type { Order } from "@/lib/types";

function fmt(n: string | null | undefined, dp = 2): string {
  if (n === null || n === undefined) return "—";
  const v = Number(n);
  if (!Number.isFinite(v)) return String(n);
  return v.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

function realizedFor(order: Order): number {
  if (!order.filled_quantity || !order.filled_avg_price) return 0;
  return Number(order.filled_quantity) * Number(order.filled_avg_price);
}

export default function TradesPage() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);
  const [flashId, setFlashId] = useState<string | null>(null);

  useEffect(() => {
    api<Order[]>("/api/trades").then(setOrders).finally(() => setLoading(false));
  }, []);

  useEventStream((evt) => {
    if (
      evt.type !== "order.placed" &&
      evt.type !== "order.copy_submitted" &&
      evt.type !== "order.copy_failed"
    ) {
      return;
    }
    const incoming = evt.order;
    setOrders((cur) => {
      const idx = cur.findIndex((o) => o.id === incoming.id);
      // Build a minimally-shaped Order from the SSE payload — `fills` is
      // unknown until we re-fetch, so default to [].
      const merged: Order = {
        id: incoming.id,
        parent_order_id: incoming.parent_order_id,
        broker_account_id: incoming.broker_account_id,
        instrument_type: incoming.instrument_type as Order["instrument_type"],
        symbol: incoming.symbol,
        side: incoming.side as Order["side"],
        order_type: incoming.order_type as Order["order_type"],
        quantity: incoming.quantity,
        limit_price: idx >= 0 ? cur[idx].limit_price : null,
        stop_price: idx >= 0 ? cur[idx].stop_price : null,
        option_expiry: idx >= 0 ? cur[idx].option_expiry : null,
        option_strike: idx >= 0 ? cur[idx].option_strike : null,
        option_right: idx >= 0 ? cur[idx].option_right : null,
        status: incoming.status as Order["status"],
        broker_order_id: incoming.broker_order_id,
        filled_quantity: incoming.filled_quantity,
        filled_avg_price: incoming.filled_avg_price,
        submitted_at: idx >= 0 ? cur[idx].submitted_at : null,
        closed_at: idx >= 0 ? cur[idx].closed_at : null,
        reject_reason: incoming.reject_reason,
        created_at: incoming.created_at ?? new Date().toISOString(),
        fills: idx >= 0 ? cur[idx].fills : [],
      };
      const next = idx >= 0
        ? [...cur.slice(0, idx), merged, ...cur.slice(idx + 1)]
        : [merged, ...cur];
      return next;
    });
    setFlashId(incoming.id);
    setTimeout(() => setFlashId((f) => (f === incoming.id ? null : f)), 2000);
  });

  if (loading) return <p style={{ color: "var(--muted)" }}>Loading trades…</p>;

  return (
    <div className="space-y-4 max-w-6xl">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Trades</h1>
        <span className="text-xs px-2 py-1 rounded" style={{ background: "rgba(34,197,94,0.12)", color: "var(--good)" }}>
          ● live
        </span>
      </div>
      <p className="text-sm" style={{ color: "var(--muted)" }}>
        Realized P&amp;L by day is on the <a href="/calendar" className="underline">Calendar</a> page.
      </p>
      <div className="overflow-x-auto rounded border" style={{ borderColor: "var(--border)" }}>
        <table className="w-full text-sm">
          <thead style={{ background: "var(--panel)" }}>
            <tr>
              {["When", "Symbol", "Type", "Side", "Qty", "Filled @", "Status", "Notional"].map(h => (
                <th key={h} className="text-left px-3 py-2 font-medium" style={{ color: "var(--muted)" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {orders.length === 0 && (
              <tr><td colSpan={8} className="px-3 py-6 text-center" style={{ color: "var(--muted)" }}>No trades yet.</td></tr>
            )}
            {orders.map(o => (
              <tr
                key={o.id}
                className="border-t transition-colors"
                style={{
                  borderColor: "var(--border)",
                  background: flashId === o.id ? "rgba(78,161,255,0.16)" : "transparent",
                }}
              >
                <td className="px-3 py-2">{new Date(o.created_at).toLocaleString()}</td>
                <td className="px-3 py-2 font-medium">
                  {o.symbol}
                  {o.instrument_type === "option" && o.option_expiry && (
                    <span className="ml-1 text-xs" style={{ color: "var(--muted)" }}>
                      {o.option_expiry} {o.option_strike} {o.option_right?.toUpperCase()[0]}
                    </span>
                  )}
                </td>
                <td className="px-3 py-2">{o.instrument_type}</td>
                <td className="px-3 py-2 uppercase" style={{ color: o.side === "buy" ? "var(--good)" : "var(--bad)" }}>{o.side}</td>
                <td className="px-3 py-2">{fmt(o.quantity, 0)}</td>
                <td className="px-3 py-2">{fmt(o.filled_avg_price, 2)}</td>
                <td className="px-3 py-2">{o.status}{o.parent_order_id ? " · copy" : ""}</td>
                <td className="px-3 py-2">{fmt(String(realizedFor(o)))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
