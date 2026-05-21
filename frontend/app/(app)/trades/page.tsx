"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { fmtDate, fmtDateTime, fmtDateTimeMs, fmtDuration } from "@/lib/format";
import { useEventStream } from "@/lib/sse";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import type { Order, OrderStatus, Position, User } from "@/lib/types";

const OPEN_STATUSES: OrderStatus[] = ["pending", "submitted", "accepted", "partially_filled"];

function fmt(n: string | null | undefined, dp = 2): string {
  if (n === null || n === undefined) return "—";
  const v = Number(n);
  if (!Number.isFinite(v)) return String(n);
  return v.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

/** Notional value of fills. For options multiply by 100 (contract multiplier). */
function notionalFor(order: Order): number {
  if (!order.filled_quantity || !order.filled_avg_price) return 0;
  const base = Number(order.filled_quantity) * Number(order.filled_avg_price);
  return order.instrument_type === "option" ? base * 100 : base;
}

/** "Expected" price the user asked for: the limit (or stop) price they set,
 *  or null for market orders. */
function expectedPrice(o: Order): string | null {
  if (o.order_type === "limit" || o.order_type === "stop_limit") return o.limit_price;
  if (o.order_type === "stop") return o.stop_price;
  return null;
}

/** Option expiry rendered as a relative day count ("in 2 days", "Today",
 *  "Expired 3d ago"). UTC-anchored so timezone offsets don't tip the count. */
function fmtExpiresIn(isoDate: string | null): { text: string; color: string } | null {
  if (!isoDate) return null;
  const target = new Date(isoDate + (isoDate.length === 10 ? "T00:00:00Z" : ""));
  if (Number.isNaN(target.getTime())) return null;
  const now = new Date();
  const t0 = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const t1 = Date.UTC(target.getUTCFullYear(), target.getUTCMonth(), target.getUTCDate());
  const d = Math.round((t1 - t0) / 86_400_000);
  // Past expiries collapse to "Expired" (in red); "Today" reads better
  // than "0"; otherwise show the raw day count.
  if (d < 0) return { text: "Expired", color: "var(--bad)" };
  if (d === 0) return { text: "Today", color: "var(--bad)" };
  if (d === 1) return { text: String(d), color: "var(--bad)" };
  return { text: String(d), color: "var(--text)" };
}

export default function TradesPage() {
  const searchParams = useSearchParams();
  // Optional ?from=YYYY-MM-DD&to=YYYY-MM-DD filter (used by Calendar drill-in).
  // `from` is inclusive, `to` is inclusive on the calendar view; we widen `to`
  // by 1 day on the API call below so trades from the entire end-date day
  // are included (the backend's filter is `< to`).
  const fromParam = searchParams?.get("from") ?? null;
  const toParam = searchParams?.get("to") ?? null;

  const [orders, setOrders] = useState<Order[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [loading, setLoading] = useState(true);
  const [flashId, setFlashId] = useState<string | null>(null);
  const [user, setUser] = useState<User | null>(null);
  // "All Orders" = everything they own, including subscriber mirrors and
  // trader orders that fanned out.
  // "My Orders" = orders private to the caller (no parent_order_id AND not
  // broadcast). Default is "all" so the user sees the full picture first.
  const [tab, setTab] = useState<"all" | "mine">("all");

  // Action UI state — tracks WHICH button on WHICH row is in flight, so only
  // that button shows "…" (not its sibling).
  const [actingFor, setActingFor] = useState<{ id: string; kind: "cancel" | "market" | "limit" } | null>(null);
  // Per-row limit-price input for the inline "Close at Limit" action.
  const [closePrices, setClosePrices] = useState<Record<string, string>>({});
  // Coalesce SSE-triggered sync-fills + reload. SSE delivers a mirror order
  // moments before Alpaca fills it; without a follow-up sync the row sits at
  // "submitted" forever (and shows a misleading Cancel button) until you
  // refresh manually.
  const reconcileTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try { await api("/api/trades/sync-fills", { method: "POST" }); } catch { /* non-blocking */ }
      if (cancelled) return;
      // Build the trades URL, narrowing it when the caller arrived from a
      // calendar tile. The backend filter is `created_at < to`, so we push
      // `to` to the day AFTER the chosen end-date to make the range inclusive.
      let tradesUrl = "/api/trades";
      if (fromParam || toParam) {
        const q = new URLSearchParams();
        if (fromParam) q.set("from", fromParam);
        if (toParam) {
          const t = new Date(toParam + "T00:00:00Z");
          t.setUTCDate(t.getUTCDate() + 1);
          q.set("to", t.toISOString().slice(0, 10));
        }
        tradesUrl = `/api/trades?${q.toString()}`;
      }
      const [o, u, p] = await Promise.all([
        api<Order[]>(tradesUrl),
        api<User>("/api/auth/me"),
        api<Position[]>("/api/positions").catch(() => [] as Position[]),
      ]);
      if (!cancelled) {
        setOrders(o);
        setUser(u);
        setPositions(p);
        setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromParam, toParam]);

  useEventStream((evt) => {
    if (
      evt.type !== "order.placed" &&
      evt.type !== "order.copy_submitted" &&
      evt.type !== "order.copy_failed" &&
      evt.type !== "order.cancelled"
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

    // If the incoming order isn't terminal yet (e.g. a fresh mirror sitting at
    // SUBMITTED), the broker is likely about to fill it within milliseconds.
    // Schedule a single sync-fills + reload to catch the real status. Coalesce
    // repeated events into one round-trip.
    const terminal = incoming.status === "filled" || incoming.status === "canceled" || incoming.status === "rejected";
    if (!terminal) {
      if (reconcileTimer.current) clearTimeout(reconcileTimer.current);
      reconcileTimer.current = setTimeout(async () => {
        try { await api("/api/trades/sync-fills", { method: "POST" }); } catch { /* ignore */ }
        try {
          const fresh = await api<Order[]>("/api/trades");
          setOrders(fresh);
        } catch { /* ignore */ }
      }, 1500);
    }
  });

  // Clear the reconcile timer on unmount so it doesn't fire against
  // a stale component.
  useEffect(() => {
    return () => { if (reconcileTimer.current) clearTimeout(reconcileTimer.current); };
  }, []);

  async function cancelOrder(id: string) {
    setActingFor({ id, kind: "cancel" });
    try {
      const updated = await api<Order>(`/api/trades/${id}/cancel`, { method: "POST" });
      setOrders(cur => cur.map(o => o.id === id ? updated : o));
      notify.success(`Order canceled: ${updated.symbol}`);
    } catch (e) {
      notify.fromError(e, "cancel failed");
    } finally {
      setActingFor(null);
    }
  }

  /** One-shot close: type=market → fires immediately, type=limit → uses the
   *  per-row price input. */
  async function closeAt(id: string, type: "market" | "limit") {
    if (type === "limit") {
      const price = closePrices[id];
      if (!price || Number(price) <= 0) {
        notify.warn("Enter a limit price");
        return;
      }
    }
    setActingFor({ id, kind: type });
    try {
      const body: Record<string, unknown> = { order_type: type };
      if (type === "limit") body.limit_price = closePrices[id];
      const newOrder = await api<Order>(`/api/trades/${id}/close`, {
        method: "POST", body: JSON.stringify(body),
      });
      setOrders(cur => [newOrder, ...cur]);
      if (type === "limit") setClosePrices(p => ({ ...p, [id]: "" }));
      notify.success(`Close placed: ${newOrder.side.toUpperCase()} ${newOrder.symbol} (${type})`);
      // Re-fetch live positions so the Close buttons hide for the contract
      // we just closed (the SELL fills almost instantly for market orders).
      api<Position[]>("/api/positions").then(setPositions).catch(() => {});
    } catch (e) {
      notify.fromError(e, "close failed");
    } finally {
      setActingFor(null);
    }
  }

  // Don't early-return — render the table shell immediately so the headers
  // are visible while the data is loading; a spinner row goes inside the body.

  return (
    // Flex column with full height so the table can claim all leftover vertical
    // space below the (optional) error banner.
    <div className="flex flex-col h-full space-y-4">
      {(fromParam || toParam) && (
        <div
          className="flex items-center justify-between gap-3 px-3 py-2 rounded border text-sm"
          style={{ borderColor: "var(--border)", background: "rgba(10,115,168,0.06)" }}
        >
          <div style={{ color: "var(--text-2)" }}>
            {"Showing trades for "}
            <strong>
              {fromParam === toParam || !toParam ? fromParam : `${fromParam} → ${toParam}`}
            </strong>
          </div>
          <Link
            href="/trades"
            prefetch={false}
            className="underline text-xs"
            style={{ color: "var(--accent)" }}
          >
            Clear filter
          </Link>
        </div>
      )}

      {(() => {
        // Live counts so each tab can show how many orders it'd surface.
        // Keep this in sync with the body-side `visibleOrders` filter.
        const mineCount = orders.filter(
          o => !o.parent_order_id && !o.fanned_out_to_subscribers
        ).length;
        const allCount = orders.length;
        const Tab = ({ k, label, count }: { k: "mine" | "all"; label: string; count: number }) => {
          const active = tab === k;
          return (
            <button
              key={k}
              type="button"
              onClick={() => setTab(k)}
              className="px-3 py-1.5 text-xs font-medium rounded-full transition-colors"
              style={{
                border: `1px solid ${active ? "rgba(10,115,168,0.4)" : "var(--border)"}`,
                background: active ? "rgba(10,115,168,0.16)" : "transparent",
                color: active ? "var(--accent)" : "var(--text-2)",
              }}
            >
              {label}{" "}
              <span style={{ color: active ? "var(--accent)" : "var(--muted)" }}>({count})</span>
            </button>
          );
        };
        return (
          <div className="flex gap-2 items-center">
            <Tab k="all" label="All Orders" count={allCount} />
            <Tab k="mine" label="My Orders" count={mineCount} />
          </div>
        );
      })()}
      {/* Table wrapper fills remaining height. min-h-0 lets it shrink within
          the flex parent so its own overflow-auto can take over. */}
      <div
        className="flex-1 min-h-0 overflow-auto rounded border"
        style={{ borderColor: "var(--border)" }}
      >
        {/* min-w-full keeps the table at least as wide as the wrapper, but
            lets it grow wider when content needs it — triggers horizontal
            scroll on the wrapper. whitespace-nowrap on every header keeps
            column widths predictable. */}
        <table className="min-w-full text-sm">
          <thead
            className="sticky top-0 z-10"
            style={{ background: "var(--panel)" }}
          >
            <tr>
              {["Symbol", "Expiry Date", "Type", "Side", "Quantity", "Actions", "Expected price", "Filled price", "Notional", "Status", "Submitted at", "Filled at", "Time Taken to Filled", "Expires in Days"].map(h => (
                <th key={h} className="text-left px-5 py-3 font-medium whitespace-nowrap" style={{ color: "var(--muted)" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={14} className="px-3 py-8 text-center" style={{ color: "var(--muted)" }}>
                  <span className="inline-flex items-center gap-2">
                    <Spinner />
                    <span>Loading orders…</span>
                  </span>
                </td>
              </tr>
            )}
            {!loading && orders.length === 0 && (
              <tr><td colSpan={14} className="px-3 py-6 text-center" style={{ color: "var(--muted)" }}>No trades yet.</td></tr>
            )}
            {(() => {
              // Only filled orders whose underlying position is still open
              // can be "closed". An option position is uniquely identified by
              // its contract (expiry+strike+right), not just the root ticker —
              // otherwise closing one AAPL call would still light up Close on
              // every AAPL row because the user holds AAPL stock.
              // Strike comes back as "200" from the order payload but "200.000"
              // from the parsed OCC on positions — normalize to a Number string
              // ("200") so the two compare cleanly. Same defensive normalize on
              // expiry just in case the date ever lands as a Date string.
              const normStrike = (s: string | null) => {
                if (s == null) return "";
                const n = Number(s);
                return Number.isFinite(n) ? String(n) : s;
              };
              const normExpiry = (s: string | null) => (s ?? "").slice(0, 10);
              const posKey = (
                acctId: string,
                instrument: string,
                symbol: string,
                expiry: string | null,
                strike: string | null,
                right: string | null,
              ) =>
                instrument === "option"
                  ? `${acctId}:OPT:${symbol.toUpperCase()}:${normExpiry(expiry)}:${normStrike(strike)}:${right ?? ""}`
                  : `${acctId}:STK:${symbol.toUpperCase()}`;

              const heldKeys = new Set(
                positions
                  .filter(p => Number(p.quantity) !== 0)
                  .map(p => posKey(
                    p.broker_account_id,
                    p.instrument_type,
                    p.symbol,
                    p.option_expiry,
                    p.option_strike,
                    p.option_right,
                  ))
              );
              // Hide rows that correspond to an open position — those live in
              // the Open Positions table on the Trade Panel. Order History
              // should be the historical record of closed/cancelled/rejected
              // activity, not duplicate the live-position view.
              const visibleOrders = orders.filter(o => {
                // Tab filter — "My Orders" = orders that are private to the
                // caller. For subscribers that means: not a mirror copied
                // from the trader (parent_order_id is null). For traders that
                // means: not broadcast to subscribers (fanned_out is false —
                // e.g. orders placed while copy was paused or with the
                // "Just me" Exit All scope). "All Orders" shows everything.
                if (tab === "mine") {
                  if (o.parent_order_id) return false;
                  if (o.fanned_out_to_subscribers) return false;
                }

                // Date-range filter (from Calendar drill-in): the backend
                // already narrowed by `created_at` via the from/to query
                // params, so we don't need to re-filter here — just bypass
                // the "hide if held" rule so opens/closes for the picked
                // day both surface.
                if (fromParam || toParam) return true;

                if (o.status !== "filled") return true;     // open / cancelled / rejected — always show
                return !heldKeys.has(posKey(
                  o.broker_account_id,
                  o.instrument_type,
                  o.symbol,
                  o.option_expiry,
                  o.option_strike,
                  o.option_right,
                ));
              });

              return visibleOrders.map(o => {
              const isOpen = OPEN_STATUSES.includes(o.status);
              const isFilled = o.status === "filled";
              const isMine = !o.parent_order_id;     // own order (not a mirror)
              const canCancel = isOpen;
              // No more Close buttons in Order History — close lives on the
              // Trade Panel's Open Positions table now.
              const canClose = false;
              return (
                <Fragment key={o.id}>
                  <tr
                    className="border-t transition-colors"
                    style={{
                      borderColor: "var(--border)",
                      background: flashId === o.id ? "var(--good-soft)" : "transparent",
                    }}
                  >
                    {/* Symbol — ticker only */}
                    <td className="px-5 py-3 font-medium">{o.symbol}</td>
                    {/* Expiry Date — absolute date for options, "—" for stocks. */}
                    <td className="px-5 py-3 whitespace-nowrap" style={{ color: o.option_expiry ? "var(--text-2)" : "var(--faint)" }}>
                      {o.option_expiry ? fmtDate(o.option_expiry) : "—"}
                    </td>

                    <td className="px-5 py-3 capitalize">{o.instrument_type}</td>
                    <td className="px-5 py-3 uppercase font-medium" style={{ color: o.side === "buy" ? "var(--good)" : "var(--bad)" }}>{o.side}</td>
                    <td className="px-5 py-3 num">{fmt(o.quantity, 0)}</td>

                    {/* Actions — inline, no expand step.
                        Open orders → [Cancel].
                        Filled own orders (trader) → [Close at Market] [limit input] [Close at Limit]. */}
                    <td className="px-5 py-3">
                      <div className="flex gap-2 items-center whitespace-nowrap">
                        {canCancel && (
                          <button
                            disabled={actingFor?.id === o.id}
                            onClick={() => cancelOrder(o.id)}
                            className="btn-danger-soft px-3 py-1 text-xs inline-flex items-center gap-1.5"
                          >
                            <span>Cancel</span>
                            {actingFor?.id === o.id && actingFor.kind === "cancel" && <Spinner />}
                          </button>
                        )}

                        {canClose && (
                          <>
                            <button
                              disabled={actingFor?.id === o.id}
                              onClick={() => closeAt(o.id, "market")}
                              className="btn-ghost px-3 py-1 text-xs inline-flex items-center gap-1.5"
                            >
                              <span>Close at Market</span>
                              {actingFor?.id === o.id && actingFor.kind === "market" && <Spinner />}
                            </button>
                            {/* Limit input + Close button — joined as one compact unit */}
                            <div className="flex items-stretch">
                              <input
                                type="number" step="0.01" min="0.01"
                                placeholder="Limit"
                                value={closePrices[o.id] ?? ""}
                                onChange={e => setClosePrices(p => ({ ...p, [o.id]: e.target.value }))}
                                className="w-20 px-2 py-1 text-xs"
                                style={{
                                  borderTopLeftRadius: "var(--r-sm)",
                                  borderBottomLeftRadius: "var(--r-sm)",
                                  borderTopRightRadius: 0,
                                  borderBottomRightRadius: 0,
                                  borderRight: "none",
                                }}
                              />
                              <button
                                disabled={actingFor?.id === o.id || !closePrices[o.id]}
                                onClick={() => closeAt(o.id, "limit")}
                                className="btn-accent-solid px-3 py-1 text-xs font-medium inline-flex items-center gap-1.5"
                                style={{
                                  borderTopLeftRadius: 0,
                                  borderBottomLeftRadius: 0,
                                  borderTopRightRadius: "var(--r-sm)",
                                  borderBottomRightRadius: "var(--r-sm)",
                                }}
                              >
                                <span>Close</span>
                                {actingFor?.id === o.id && actingFor.kind === "limit" && <Spinner />}
                              </button>
                            </div>
                          </>
                        )}

                        {!canCancel && !canClose && (
                          <span className="text-xs" style={{ color: "var(--faint)" }}>—</span>
                        )}
                      </div>
                    </td>

                    {/* Expected price — what the user asked for (limit/stop) */}
                    <td className="px-5 py-3 num">{fmt(expectedPrice(o), 2)}</td>
                    {/* Filled price — actual avg execution price */}
                    <td className="px-5 py-3 num">{fmt(o.filled_avg_price, 2)}</td>
                    {/* Notional — qty × price (× 100 for options) */}
                    <td className="px-5 py-3 num">
                      {notionalFor(o)
                        ? fmt(String(notionalFor(o)))
                        : <span style={{ color: "var(--faint)" }}>—</span>}
                    </td>
                    {/* Status — color-coded pill */}
                    <td className="px-5 py-3">
                      <span
                        className="text-[11px] uppercase tracking-wider px-2 py-[4px] rounded whitespace-nowrap font-medium"
                        style={{
                          background:
                            o.status === "filled"     ? "var(--good-soft)" :
                            o.status === "rejected"   ? "var(--bad-soft)"  :
                            o.status === "canceled"   ? "rgba(255,255,255,0.04)" :
                                                        "rgba(10,115,168,0.10)",
                          color:
                            o.status === "filled"     ? "var(--good)" :
                            o.status === "rejected"   ? "var(--bad)"  :
                            o.status === "canceled"   ? "var(--muted)" :
                                                        "var(--accent)",
                        }}
                      >
                        {o.status}{o.parent_order_id ? " · copy" : ""}
                      </span>
                    </td>
                    {/* Submitted at — fallback to created_at for orders that
                        never reached the broker (rejected pre-submit) */}
                    <td className="px-5 py-3 whitespace-nowrap" style={{ color: "var(--muted)" }}>
                      {fmtDateTimeMs(o.submitted_at ?? o.created_at, "America/New_York")}
                    </td>
                    {/* Filled at — latest fill timestamp, or closed_at as fallback
                        for terminal-but-fillless rows (rejected etc). */}
                    {(() => {
                      const lastFillAt = o.fills?.length
                        ? o.fills.reduce((a, b) => (a.filled_at > b.filled_at ? a : b)).filled_at
                        : null;
                      const fillTs = lastFillAt ?? (o.status === "filled" ? o.closed_at : null);
                      const submittedTs = o.submitted_at ?? o.created_at;
                      return (
                        <>
                          <td className="px-5 py-3 whitespace-nowrap" style={{ color: "var(--muted)" }}>
                            {fillTs ? fmtDateTimeMs(fillTs, "America/New_York") : <span style={{ color: "var(--faint)" }}>—</span>}
                          </td>
                          <td className="px-5 py-3 whitespace-nowrap num" style={{ color: fillTs ? "var(--text-2)" : "var(--faint)" }}>
                            {fillTs ? fmtDuration(submittedTs, fillTs) : "—"}
                          </td>
                        </>
                      );
                    })()}
                    {/* Expires in — option contract expiry rendered as a
                        relative day count; "—" for stocks. */}
                    {(() => {
                      const exp = o.instrument_type === "option" ? fmtExpiresIn(o.option_expiry) : null;
                      return (
                        <td className="px-5 py-3 whitespace-nowrap" style={{ color: exp ? exp.color : "var(--faint)" }}>
                          {exp ? exp.text : "—"}
                        </td>
                      );
                    })()}
                  </tr>
                </Fragment>
              );
            });
            })()}
          </tbody>
        </table>
      </div>
    </div>
  );
}
