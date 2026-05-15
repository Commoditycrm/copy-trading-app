"use client";

import { Fragment, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, forwardRef } from "react";
import { api } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";
import type { Order, Position } from "@/lib/types";

function fmtNum(n: string | null | undefined, dp = 2): string {
  if (n === null || n === undefined || n === "") return "—";
  const v = Number(n);
  if (!Number.isFinite(v)) return String(n);
  return v.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

function fmtSignedMoney(n: string | null | undefined): { text: string; sign: 1 | -1 | 0 | null } {
  if (n === null || n === undefined || n === "") return { text: "—", sign: null };
  const v = Number(n);
  if (!Number.isFinite(v)) return { text: String(n), sign: null };
  return {
    text: v.toLocaleString(undefined, { style: "currency", currency: "USD" }),
    sign: v === 0 ? 0 : v > 0 ? 1 : -1,
  };
}

function posKey(p: Position): string {
  return `${p.broker_account_id}:${p.broker_symbol}`;
}

/** Days from today (UTC midnight) until an ISO date. Negative if past. */
function daysUntil(isoDate: string): number {
  const target = new Date(isoDate + (isoDate.length === 10 ? "T00:00:00Z" : ""));
  if (Number.isNaN(target.getTime())) return NaN;
  const today = new Date();
  const t0 = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  const t1 = Date.UTC(target.getUTCFullYear(), target.getUTCMonth(), target.getUTCDate());
  return Math.round((t1 - t0) / 86_400_000);
}

function fmtExpiresIn(isoDate: string | null): { text: string; color: string } {
  if (!isoDate) return { text: "—", color: "var(--faint)" };
  const d = daysUntil(isoDate);
  if (!Number.isFinite(d)) return { text: "—", color: "var(--faint)" };
  // "Today" reads better than "0"; everything else stays numeric.
  if (d === 0) return { text: "Today", color: "var(--bad)" };
  if (d === 1) return { text: String(d), color: "var(--bad)" };
  return { text: String(d), color: "var(--text)" };
}

export interface OpenPositionsTableHandle {
  /** Force a refresh from /api/positions. Call after placing/exiting orders. */
  refresh: () => Promise<void>;
}

export const OpenPositionsTable = forwardRef<OpenPositionsTableHandle, { className?: string }>(
  function OpenPositionsTable({ className }, ref) {
    const [positions, setPositions] = useState<Position[]>([]);
    const [orders, setOrders] = useState<Order[]>([]);
    const [loading, setLoading] = useState(true);
    const [closing, setClosing] = useState<{ key: string; kind: "market" | "limit" } | null>(null);
    const [closeLimitPrices, setCloseLimitPrices] = useState<Record<string, string>>({});
    // Per-row close size as a percentage of the held quantity. Defaults to 100%.
    const [closePercents, setClosePercents] = useState<Record<string, number>>({});
    // Filter: default to options since that's the most common workflow here.
    const [filter, setFilter] = useState<"all" | "stock" | "option">("option");

    /** Translate a chosen percentage into a concrete close quantity. Options
     *  trade in whole contracts; stocks allow up to 6 decimals (Alpaca's
     *  fractional precision). Returns null if the result rounds to zero. */
    function quantityForPercent(p: Position, pct: number): number | null {
      const total = Math.abs(Number(p.quantity));
      if (!Number.isFinite(total) || total <= 0) return null;
      let qty = total * (pct / 100);
      if (p.instrument_type === "option") qty = Math.floor(qty);
      else qty = Math.round(qty * 1e6) / 1e6;
      return qty > 0 ? qty : null;
    }

    const refresh = useCallback(async () => {
      try {
        const [pos, ords] = await Promise.all([
          api<Position[]>("/api/positions"),
          api<Order[]>("/api/trades").catch(() => [] as Order[]),
        ]);
        setPositions(pos);
        setOrders(ords);
      } catch (e) {
        notify.fromError(e, "failed to load positions");
      } finally {
        setLoading(false);
      }
    }, []);

    useEffect(() => { refresh(); }, [refresh]);

    useImperativeHandle(ref, () => ({ refresh }), [refresh]);

    // Real-time: any order event for this user (own placement, mirror from a
    // followed trader, cancellation, etc.) is a reason to re-check positions.
    // Debounce so a burst of fanout events fires one network round-trip, and
    // give the broker a moment to actually fill before we ask for the
    // post-fill state.
    const ssEventTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    useEventStream((evt) => {
      if (
        evt.type !== "order.placed" &&
        evt.type !== "order.copy_submitted" &&
        evt.type !== "order.copy_failed" &&
        evt.type !== "order.cancelled"
      ) return;
      if (ssEventTimer.current) clearTimeout(ssEventTimer.current);
      ssEventTimer.current = setTimeout(() => { refresh(); }, 1500);
    });
    useEffect(() => () => { if (ssEventTimer.current) clearTimeout(ssEventTimer.current); }, []);

    async function closePosition(p: Position, type: "market" | "limit") {
      const key = posKey(p);
      if (type === "limit") {
        const price = closeLimitPrices[key];
        if (!price || Number(price) <= 0) {
          notify.warn("Enter a limit price");
          return;
        }
      }
      const pct = closePercents[key] ?? 100;
      const qty = quantityForPercent(p, pct);
      if (qty == null) {
        notify.warn(`Can't close ${pct}% of this position — would round to zero.`);
        return;
      }
      setClosing({ key, kind: type });
      try {
        const body: Record<string, unknown> = { order_type: type };
        if (pct < 100) body.quantity = String(qty);   // 100% lets the backend default to full size
        if (type === "limit") body.limit_price = closeLimitPrices[key];
        const order = await api<Order>(
          `/api/positions/${encodeURIComponent(p.broker_symbol)}/close?broker_account_id=${p.broker_account_id}`,
          { method: "POST", body: JSON.stringify(body) },
        );
        notify.success(`Close placed: ${order.side.toUpperCase()} ${order.symbol} ×${qty} (${type})`);
        if (type === "limit") setCloseLimitPrices(s => ({ ...s, [key]: "" }));
        refresh();
      } catch (e) {
        notify.fromError(e, "close failed");
      } finally {
        setClosing(null);
      }
    }

    // Map contract identity → most recent FILLED entry order (so we can show
    // when the position was opened). Same contract may have multiple buys;
    // we pick the latest fill as the representative "opened at".
    const orderTimestamps = useMemo(() => {
      const normStrike = (s: string | null) => {
        if (s == null) return "";
        const n = Number(s);
        return Number.isFinite(n) ? String(n) : s;
      };
      const normExpiry = (s: string | null) => (s ?? "").slice(0, 10);
      const key = (
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

      const byKey = new Map<string, { submitted_at: string | null; filled_at: string | null }>();
      for (const o of orders) {
        if (o.status !== "filled" && o.status !== "partially_filled") continue;
        const k = key(o.broker_account_id, o.instrument_type, o.symbol, o.option_expiry, o.option_strike, o.option_right);
        const lastFillAt = o.fills?.length
          ? o.fills.reduce((a, b) => (a.filled_at > b.filled_at ? a : b)).filled_at
          : (o.status === "filled" ? o.closed_at : null);
        const prev = byKey.get(k);
        // Keep the latest record per contract (by fill time).
        if (!prev || (lastFillAt ?? "") > (prev.filled_at ?? "")) {
          byKey.set(k, { submitted_at: o.submitted_at ?? o.created_at, filled_at: lastFillAt });
        }
      }
      return { byKey, key };
    }, [orders]);

    const visible = filter === "all"
      ? positions
      : positions.filter(p => p.instrument_type === filter);

    const counts = {
      all: positions.length,
      option: positions.filter(p => p.instrument_type === "option").length,
      stock: positions.filter(p => p.instrument_type === "stock").length,
    };

    const tabBtn = (key: "option" | "stock" | "all", label: string) => {
      const active = filter === key;
      return (
        <button
          key={key}
          type="button"
          onClick={() => setFilter(key)}
          className="px-3 py-1.5 text-xs font-medium rounded-full transition-colors"
          style={{
            border: `1px solid ${active ? "rgba(10,115,168,0.35)" : "var(--border)"}`,
            background: active ? "rgba(10,115,168,0.16)" : "transparent",
            color: active ? "var(--accent)" : "var(--text-2)",
          }}
        >
          {label}{" "}
          <span style={{ color: active ? "var(--accent)" : "var(--muted)" }}>
            ({counts[key]})
          </span>
        </button>
      );
    };

    return (
      <div className={className}>
        <div className="flex items-center justify-between mb-3 gap-2 flex-wrap">
          <div className="flex items-center gap-2">
            {tabBtn("option", "Options")}
            {tabBtn("stock", "Stocks")}
            {tabBtn("all", "All")}
          </div>
          {/* <span className="text-xs" style={{ color: "var(--muted)" }}>
            {loading ? "loading…" : `${visible.length} shown`}
          </span> */}
        </div>

        <div className="overflow-auto rounded border" style={{ borderColor: "var(--border)" }}>
          <table className="min-w-full text-sm">
            <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
              <tr>
                {["Symbol", "Type", "Side", "Quantity", "Close %", "Actions", "Avg entry", "Current price", "Market value", "Unrealized P&L", "Submitted at", "Filled at", "Expires in Days"].map(h => (
                  <th key={h} className="text-left px-5 py-3 font-medium whitespace-nowrap" style={{ color: "var(--muted)" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td colSpan={13} className="px-3 py-8 text-center" style={{ color: "var(--muted)" }}>
                    <span className="inline-flex items-center gap-2">
                      <Spinner />
                      <span>Loading positions…</span>
                    </span>
                  </td>
                </tr>
              )}
              {!loading && visible.length === 0 && (
                <tr>
                  <td colSpan={13} className="px-3 py-6 text-center" style={{ color: "var(--muted)" }}>
                    {positions.length === 0
                      ? "No open positions."
                      : filter === "option" ? "No open option positions."
                      : "No open stock positions."}
                  </td>
                </tr>
              )}
              {visible.map(p => {
                const key = posKey(p);
                const qtyNum = Number(p.quantity);
                const isLong = qtyNum > 0;
                const pnl = fmtSignedMoney(p.unrealized_pnl);
                const inFlight = closing?.key === key;
                return (
                  <Fragment key={key}>
                    <tr className="border-t" style={{ borderColor: "var(--border)" }}>
                      <td className="px-5 py-3 font-medium">{p.symbol}</td>
                      <td className="px-5 py-3 capitalize">{p.instrument_type}</td>
                      <td
                        className="px-5 py-3 uppercase font-medium"
                        style={{ color: isLong ? "var(--good)" : "var(--bad)" }}
                      >
                        {isLong ? "long" : "short"}
                      </td>
                      <td className="px-5 py-3 num">{fmtNum(String(Math.abs(qtyNum)), 0)}</td>
                      {/* Close % — pick a fraction of the position to close.
                          Pills that would round to zero (e.g. 25% of one
                          contract) are disabled. */}
                      <td className="px-5 py-3">
                        <div className="flex gap-1">
                          {[25, 50, 75, 100].map(pct => {
                            const computedQty = quantityForPercent(p, pct);
                            const disabled = computedQty == null;
                            const selected = (closePercents[key] ?? 100) === pct;
                            return (
                              <button
                                key={pct}
                                type="button"
                                disabled={disabled}
                                onClick={() => setClosePercents(s => ({ ...s, [key]: pct }))}
                                title={disabled ? "Too small to close at this %" : `Close ${pct}% (×${computedQty})`}
                                className="px-2 py-0.5 text-[10px] rounded transition-colors"
                                style={{
                                  border: `1px solid ${selected ? "rgba(10,115,168,0.4)" : "var(--border)"}`,
                                  background: selected ? "rgba(10,115,168,0.16)" : "transparent",
                                  color: disabled ? "var(--faint)" : selected ? "var(--accent)" : "var(--text-2)",
                                  cursor: disabled ? "not-allowed" : "pointer",
                                  opacity: disabled ? 0.5 : 1,
                                }}
                              >
                                {pct}%
                              </button>
                            );
                          })}
                        </div>
                      </td>
                      <td className="px-5 py-3">
                        <div className="flex gap-2 items-center whitespace-nowrap">
                          <button
                            disabled={inFlight}
                            onClick={() => closePosition(p, "market")}
                            className="btn-ghost px-3 py-1 text-xs inline-flex items-center gap-1.5"
                          >
                            <span>Close at Market</span>
                            {inFlight && closing.kind === "market" && <Spinner />}
                          </button>
                          <div className="flex items-stretch">
                            <input
                              type="number" step="0.01" min="0.01"
                              placeholder="Limit"
                              value={closeLimitPrices[key] ?? ""}
                              onChange={e => setCloseLimitPrices(s => ({ ...s, [key]: e.target.value }))}
                              className="w-20 px-2 py-1 text-xs border"
                              style={{
                                borderColor: "var(--border)",
                                background: "var(--bg)",
                                borderTopLeftRadius: "var(--r-sm)",
                                borderBottomLeftRadius: "var(--r-sm)",
                                borderTopRightRadius: 0,
                                borderBottomRightRadius: 0,
                                borderRight: "none",
                              }}
                            />
                            <button
                              disabled={inFlight || !closeLimitPrices[key]}
                              onClick={() => closePosition(p, "limit")}
                              className="btn-accent-solid px-3 py-1 text-xs font-medium inline-flex items-center gap-1.5"
                              style={{
                                borderTopLeftRadius: 0,
                                borderBottomLeftRadius: 0,
                                borderTopRightRadius: "var(--r-sm)",
                                borderBottomRightRadius: "var(--r-sm)",
                              }}
                            >
                              <span>Close</span>
                              {inFlight && closing.kind === "limit" && <Spinner />}
                            </button>
                          </div>
                        </div>
                      </td>
                      <td className="px-5 py-3 num">{fmtNum(p.avg_entry_price, 2)}</td>
                      <td className="px-5 py-3 num">{fmtNum(p.current_price, 2)}</td>
                      <td className="px-5 py-3 num">{fmtNum(p.market_value, 2)}</td>
                      <td
                        className="px-5 py-3 num"
                        style={{ color: pnl.sign === 1 ? "var(--good)" : pnl.sign === -1 ? "var(--bad)" : "var(--muted)" }}
                      >
                        {pnl.text}
                      </td>
                      {(() => {
                        const t = orderTimestamps.byKey.get(orderTimestamps.key(
                          p.broker_account_id, p.instrument_type, p.symbol,
                          p.option_expiry, p.option_strike, p.option_right,
                        ));
                        const sub = t?.submitted_at ?? null;
                        const fill = t?.filled_at ?? null;
                        return (
                          <>
                            <td className="px-5 py-3 whitespace-nowrap" style={{ color: "var(--muted)" }}>
                              {sub ? fmtDateTime(sub) : <span style={{ color: "var(--faint)" }}>—</span>}
                            </td>
                            <td className="px-5 py-3 whitespace-nowrap" style={{ color: "var(--muted)" }}>
                              {fill ? fmtDateTime(fill) : <span style={{ color: "var(--faint)" }}>—</span>}
                            </td>
                          </>
                        );
                      })()}
                      {(() => {
                        const exp = p.instrument_type === "option" ? fmtExpiresIn(p.option_expiry) : null;
                        return (
                          <td className="px-5 py-3 whitespace-nowrap" style={{ color: exp ? exp.color : "var(--faint)" }}>
                            {exp ? exp.text : "—"}
                          </td>
                        );
                      })()}
                    </tr>
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    );
  },
);
