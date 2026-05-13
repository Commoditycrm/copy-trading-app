"use client";

import { Fragment, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useEventStream } from "@/lib/sse";
import type { Order, OrderStatus, User } from "@/lib/types";

const OPEN_STATUSES: OrderStatus[] = ["pending", "submitted", "accepted", "partially_filled"];

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
  const [user, setUser] = useState<User | null>(null);

  // Action UI state — keyed by order id
  const [actingId, setActingId] = useState<string | null>(null);    // in-flight cancel/close
  const [closeFor, setCloseFor] = useState<string | null>(null);    // which row is expanded
  const [closeType, setCloseType] = useState<"market" | "limit">("market");
  const [closePrice, setClosePrice] = useState("");
  const [actionErr, setActionErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try { await api("/api/trades/sync-fills", { method: "POST" }); } catch { /* non-blocking */ }
      if (cancelled) return;
      const [o, u] = await Promise.all([
        api<Order[]>("/api/trades"),
        api<User>("/api/auth/me"),
      ]);
      if (!cancelled) {
        setOrders(o);
        setUser(u);
        setLoading(false);
      }
    })();
    return () => { cancelled = true; };
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

  async function cancelOrder(id: string) {
    if (!confirm("Cancel this open order?")) return;
    setActingId(id); setActionErr(null);
    try {
      const updated = await api<Order>(`/api/trades/${id}/cancel`, { method: "POST" });
      setOrders(cur => cur.map(o => o.id === id ? updated : o));
    } catch (e) {
      setActionErr(e instanceof ApiError ? String(e.detail) : "cancel failed");
    } finally {
      setActingId(null);
    }
  }

  function openCloseFor(id: string) {
    setCloseFor(id);
    setCloseType("market");
    setClosePrice("");
    setActionErr(null);
  }

  async function submitClose(id: string) {
    setActingId(id); setActionErr(null);
    try {
      const body: Record<string, unknown> = { order_type: closeType };
      if (closeType === "limit") body.limit_price = closePrice;
      const newOrder = await api<Order>(`/api/trades/${id}/close`, {
        method: "POST", body: JSON.stringify(body),
      });
      // Add the new reverse order to the top of the list; original stays as-is.
      setOrders(cur => [newOrder, ...cur]);
      setCloseFor(null);
    } catch (e) {
      setActionErr(e instanceof ApiError ? String(e.detail) : "close failed");
    } finally {
      setActingId(null);
    }
  }

  if (loading) return <p style={{ color: "var(--muted)" }}>Loading trades…</p>;

  return (
    <div className="space-y-4 max-w-6xl">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Order History</h1>
        <span className="text-xs px-2 py-1 rounded-full" style={{ background: "var(--good-soft)", color: "var(--good)", border: "1px solid rgba(182,255,60,0.25)" }}>
          ● live
        </span>
      </div>
      <p className="text-sm" style={{ color: "var(--muted)" }}>
        Realized P&amp;L by day is on the <a href="/calendar" className="underline">Calendar</a> page.
      </p>

      {actionErr && (
        <div className="text-sm p-3 rounded" style={{ background: "var(--bad-soft)", color: "var(--bad)" }}>
          {actionErr}
        </div>
      )}

      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead style={{ background: "var(--panel-2)" }}>
            <tr>
              {["When", "Symbol", "Type", "Side", "Qty", "Filled @", "Status", "Notional", "Actions"].map(h => (
                <th key={h} className="text-left px-3 py-2 font-medium" style={{ color: "var(--muted)" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {orders.length === 0 && (
              <tr><td colSpan={9} className="px-3 py-6 text-center" style={{ color: "var(--muted)" }}>No trades yet.</td></tr>
            )}
            {orders.map(o => {
              const isOpen = OPEN_STATUSES.includes(o.status);
              const isFilled = o.status === "filled";
              const isMine = !o.parent_order_id;     // own order (not a mirror)
              const canCancel = isOpen;
              const canClose = isFilled && user?.role === "trader" && isMine;
              const expanded = closeFor === o.id;
              return (
                <Fragment key={o.id}>
                  <tr
                    className="border-t transition-colors"
                    style={{
                      borderColor: "var(--border)",
                      background: flashId === o.id ? "var(--good-soft)" : "transparent",
                    }}
                  >
                    <td className="px-3 py-2 whitespace-nowrap">{new Date(o.created_at).toLocaleString()}</td>
                    <td className="px-3 py-2 font-medium">
                      {o.symbol}
                      {o.instrument_type === "option" && o.option_expiry && (
                        <span className="ml-1 text-xs" style={{ color: "var(--muted)" }}>
                          {o.option_expiry} {o.option_strike} {o.option_right?.toUpperCase()[0]}
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2">{o.instrument_type}</td>
                    <td className="px-3 py-2 uppercase font-medium" style={{ color: o.side === "buy" ? "var(--good)" : "var(--bad)" }}>{o.side}</td>
                    <td className="px-3 py-2 num">{fmt(o.quantity, 0)}</td>
                    <td className="px-3 py-2 num">{fmt(o.filled_avg_price, 2)}</td>
                    <td className="px-3 py-2">
                      <span className="text-xs uppercase tracking-wider">
                        {o.status}{o.parent_order_id ? " · copy" : ""}
                      </span>
                    </td>
                    <td className="px-3 py-2 num">{fmt(String(realizedFor(o)))}</td>
                    <td className="px-3 py-2">
                      <div className="flex gap-2 items-center">
                        {canCancel && (
                          <button
                            disabled={actingId === o.id}
                            onClick={() => cancelOrder(o.id)}
                            className="px-3 py-1 text-xs rounded-full"
                            style={{
                              border: "1px solid rgba(255,107,107,0.4)",
                              color: "var(--bad)", background: "var(--bad-soft)",
                            }}
                          >
                            {actingId === o.id ? "…" : "Cancel"}
                          </button>
                        )}
                        {canClose && !expanded && (
                          <button
                            onClick={() => openCloseFor(o.id)}
                            className="btn-ghost px-3 py-1 text-xs"
                          >
                            Close
                          </button>
                        )}
                        {!canCancel && !canClose && (
                          <span className="text-xs" style={{ color: "var(--faint)" }}>—</span>
                        )}
                      </div>
                    </td>
                  </tr>

                  {/* Close form — inline expansion */}
                  {expanded && (
                    <tr style={{ borderTop: "1px solid var(--border)", background: "var(--panel)" }}>
                      <td colSpan={9} className="px-4 py-3">
                        <div className="flex flex-wrap items-center gap-3">
                          <span className="text-xs uppercase tracking-wider" style={{ color: "var(--muted)" }}>
                            Close {o.symbol} ({fmt(o.filled_quantity, 0)} {o.side === "buy" ? "→ SELL" : "→ BUY"})
                          </span>

                          <div className="flex gap-1 p-0.5 rounded-full" style={{ border: "1px solid var(--border)", background: "var(--bg-tint)" }}>
                            <button
                              type="button" onClick={() => setCloseType("market")}
                              className="px-3 py-1 text-xs rounded-full transition-colors"
                              style={{
                                background: closeType === "market" ? "var(--grad-accent)" : "transparent",
                                color: closeType === "market" ? "var(--accent-ink)" : "var(--text-2)",
                                fontWeight: closeType === "market" ? 600 : 500,
                              }}
                            >Market</button>
                            <button
                              type="button" onClick={() => setCloseType("limit")}
                              className="px-3 py-1 text-xs rounded-full transition-colors"
                              style={{
                                background: closeType === "limit" ? "var(--grad-accent)" : "transparent",
                                color: closeType === "limit" ? "var(--accent-ink)" : "var(--text-2)",
                                fontWeight: closeType === "limit" ? 600 : 500,
                              }}
                            >Limit</button>
                          </div>

                          {closeType === "limit" && (
                            <input
                              type="number" step="0.01" min="0.01" placeholder="Limit price"
                              value={closePrice} onChange={e => setClosePrice(e.target.value)}
                              className="w-32 px-2 py-1 text-sm"
                            />
                          )}

                          <button
                            disabled={actingId === o.id || (closeType === "limit" && !closePrice)}
                            onClick={() => submitClose(o.id)}
                            className="btn-primary px-4 py-1.5 text-xs"
                          >
                            {actingId === o.id ? "Placing…" : "Place close"}
                          </button>
                          <button
                            onClick={() => setCloseFor(null)}
                            className="btn-ghost px-3 py-1.5 text-xs"
                          >
                            Cancel
                          </button>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
