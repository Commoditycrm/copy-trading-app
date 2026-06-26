"use client";

import { Fragment, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, forwardRef } from "react";
import { motion } from "framer-motion";
import { ArrowDown, ArrowUp, ChevronsUpDown, Layers, Search, TrendingDown, TrendingUp, X } from "lucide-react";
import { api } from "@/lib/api";
import { fmtDate, fmtDateTimeMs, fmtDuration, fmtUsd, fmtSignedUsd } from "@/lib/format";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";
import { AnimatedNumber } from "@/components/dashboard/AnimatedNumber";
import { InlineBracketCell } from "@/components/InlineBracketCell";
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
  // Past expiries collapse to "Expired" (in red); "Today" reads better
  // than "0"; otherwise show the raw day count.
  if (d < 0) return { text: "Expired", color: "var(--bad)" };
  if (d === 0) return { text: "Today", color: "var(--bad)" };
  if (d === 1) return { text: String(d), color: "var(--bad)" };
  return { text: String(d), color: "var(--text)" };
}

// ── Sorting ───────────────────────────────────────────────────────────────
type SortKey =
  | "symbol" | "quantity" | "avg_entry_price" | "current_price"
  | "market_value" | "unrealized_pnl" | "expires";

function sortValue(p: Position, key: SortKey): number | string {
  switch (key) {
    case "symbol": return p.symbol.toUpperCase();
    case "quantity": return Math.abs(Number(p.quantity)) || 0;
    case "avg_entry_price": return Number(p.avg_entry_price) || 0;
    case "current_price": return Number(p.current_price) || 0;
    case "market_value": return Number(p.market_value) || 0;
    case "unrealized_pnl": return Number(p.unrealized_pnl) || 0;
    case "expires": return p.option_expiry ? daysUntil(p.option_expiry) : Number.POSITIVE_INFINITY;
  }
}

export interface OpenPositionsTableHandle {
  /** Force a refresh from /api/positions. Call after placing/exiting orders. */
  refresh: () => Promise<void>;
}

export const OpenPositionsTable = forwardRef<OpenPositionsTableHandle, { className?: string; fillHeight?: boolean }>(
  function OpenPositionsTable({ className, fillHeight }, ref) {
    const [positions, setPositions] = useState<Position[]>([]);
    const [orders, setOrders] = useState<Order[]>([]);
    const [loading, setLoading] = useState(true);
    const [closing, setClosing] = useState<{ key: string; kind: "market" | "limit" } | null>(null);
    const [closeLimitPrices, setCloseLimitPrices] = useState<Record<string, string>>({});
    // Per-row close size as a percentage of the held quantity. Defaults to 100%.
    const [closePercents, setClosePercents] = useState<Record<string, number>>({});
    // Filter: default to options since that's the most common workflow here.
    const [filter, setFilter] = useState<"all" | "stock" | "option">("option");
    // Presentational only — symbol search + column sort.
    const [search, setSearch] = useState("");
    const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" } | null>(null);

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
    //
    // We fire multiple staggered refreshes per event burst, not just one,
    // because subscribers DON'T have their own broker listener running
    // (backend listeners are TRADER-only — see snaptrade_listener.start_all_listeners
    // and trade_listener.start_all_listeners). That means the only SSE
    // they ever receive for a mirror order is `order.copy_submitted`,
    // emitted the instant we hand the order to the broker — BEFORE it
    // fills. A single 1.5s refresh almost always misses the fill, so
    // the user has to manually reload the page to see the new position.
    //
    // Staggered schedule (1.5s, 6s, 18s, 35s) catches:
    //   - immediate-fill paper accounts (1.5s)
    //   - typical live-broker fill latency (6s)
    //   - laggy SnapTrade / multi-leg fills (18-35s)
    //
    // Burst-debounce: any new event clears the prior schedule so a flurry
    // of fanout events fires ONE schedule, not N. The cleanup on unmount
    // clears every pending timer.
    const SCHEDULE_MS = [1_500, 6_000, 18_000, 35_000] as const;
    const ssTimers = useRef<ReturnType<typeof setTimeout>[]>([]);
    const clearTimers = useCallback(() => {
      for (const t of ssTimers.current) clearTimeout(t);
      ssTimers.current = [];
    }, []);
    useEventStream((evt) => {
      if (
        evt.type !== "order.placed" &&
        evt.type !== "order.copy_submitted" &&
        evt.type !== "order.copy_failed" &&
        evt.type !== "order.cancelled" &&
        // pnl_poller fires this when the per-position TP/SL enforcer
        // closes a position at the broker. Without listening for it,
        // the closed position lingers in the table until the user
        // manually refreshes — even though the broker close has
        // already been placed.
        evt.type !== "position.auto_closed"
      ) return;
      clearTimers();
      for (const ms of SCHEDULE_MS) {
        ssTimers.current.push(setTimeout(() => { refresh(); }, ms));
      }
    });
    useEffect(() => () => { clearTimers(); }, [clearTimers]);

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

      const byKey = new Map<string, {
        order_id: string;                          // entry-order id (for bracket modify)
        side: Order["side"];                       // entry-order side (drives TP/SL % direction)
        parent_order_id: string | null;            // set → this is a copied mirror entry
        submitted_at: string | null;
        filled_at: string | null;
        filled_avg_price: string | null;
        // % anchor. For the TRADER'S OWN entry, prefer limit_price — the same
        // number the Trade Panel used to convert "TP 10% / SL 5%" → absolute
        // prices, so reversing it round-trips exactly. For a COPIED MIRROR
        // (parent_order_id set) the exits are re-anchored on the subscriber's
        // actual FILL, so the % must be reversed against filled_avg_price to
        // match what fires — and to match the Order History display. See the
        // entryPrice selection in the render below.
        limit_price: string | null;
        take_profit_price: string | null;
        stop_loss_price: string | null;
        take_profit_pct: string | null;       // copied-bracket intent (mirrors)
        stop_loss_pct: string | null;
      }>();
      for (const o of orders) {
        if (o.status !== "filled" && o.status !== "partially_filled") continue;
        // Skip orphan orders (broker_account_id is null because the broker
        // was disconnected after the trade) — they can't match any current
        // position. Including them with a "" key would corrupt dedup keys.
        if (!o.broker_account_id) continue;
        // Bracket-exit legs (TP/SL closes) are NOT the entry — they'd
        // overwrite the real entry's id with their own and break the
        // bracket-modify UI. Skip them; their parent is already in the loop.
        if (o.bracket_parent_id) continue;
        const k = key(o.broker_account_id, o.instrument_type, o.symbol, o.option_expiry, o.option_strike, o.option_right);
        const lastFillAt = o.fills?.length
          ? o.fills.reduce((a, b) => (a.filled_at > b.filled_at ? a : b)).filled_at
          : (o.status === "filled" ? o.closed_at : null);
        const prev = byKey.get(k);
        // Keep the latest record per contract (by fill time).
        if (!prev || (lastFillAt ?? "") > (prev.filled_at ?? "")) {
          byKey.set(k, {
            order_id: o.id,
            side: o.side,
            parent_order_id: o.parent_order_id,
            submitted_at: o.submitted_at ?? o.created_at,
            filled_at: lastFillAt,
            filled_avg_price: o.filled_avg_price,
            limit_price: o.limit_price,
            take_profit_price: o.take_profit_price,
            stop_loss_price: o.stop_loss_price,
            take_profit_pct: o.take_profit_pct ?? null,
            stop_loss_pct: o.stop_loss_pct ?? null,
          });
        }
      }
      return { byKey, key };
    }, [orders]);

    const counts = {
      all: positions.length,
      option: positions.filter(p => p.instrument_type === "option").length,
      stock: positions.filter(p => p.instrument_type === "stock").length,
    };

    // type filter → symbol search → sort (all presentational)
    const visible = useMemo(() => {
      const byType = filter === "all" ? positions : positions.filter(p => p.instrument_type === filter);
      const q = search.trim().toUpperCase();
      const bySearch = q ? byType.filter(p => p.symbol.toUpperCase().includes(q)) : byType;
      if (!sort) return bySearch;
      const arr = [...bySearch];
      arr.sort((a, b) => {
        const va = sortValue(a, sort.key);
        const vb = sortValue(b, sort.key);
        const cmp = typeof va === "string"
          ? va.localeCompare(vb as string)
          : (va as number) - (vb as number);
        return sort.dir === "asc" ? cmp : -cmp;
      });
      return arr;
    }, [positions, filter, search, sort]);

    // summary over the currently-visible rows
    const summary = useMemo(() => {
      let mv = 0, pnl = 0, longs = 0, shorts = 0;
      for (const p of visible) {
        mv += Number(p.market_value) || 0;
        pnl += Number(p.unrealized_pnl) || 0;
        if (Number(p.quantity) >= 0) longs++; else shorts++;
      }
      return { mv, pnl, longs, shorts, count: visible.length };
    }, [visible]);

    function toggleSort(key: SortKey) {
      setSort(prev => {
        if (!prev || prev.key !== key) return { key, dir: "asc" };
        if (prev.dir === "asc") return { key, dir: "desc" };
        return null;
      });
    }

    const tabBtn = (key: "option" | "stock" | "all", label: string) => {
      const active = filter === key;
      return (
        <button
          key={key}
          type="button"
          onClick={() => setFilter(key)}
          className="px-3 py-1.5 text-xs font-medium rounded-full transition-colors focus-ring"
          style={{
            border: `1px solid ${active ? "rgba(10,115,168,0.35)" : "var(--border)"}`,
            background: active ? "var(--nav-active-bg)" : "transparent",
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

    const SortIcon = ({ k }: { k: SortKey }) => {
      if (!sort || sort.key !== k) return <ChevronsUpDown size={12} style={{ opacity: 0.4 }} />;
      return sort.dir === "asc" ? <ArrowUp size={12} /> : <ArrowDown size={12} />;
    };

    const Th = ({ label, sortKey, className: thc = "" }: { label: string; sortKey?: SortKey; className?: string }) => {
      const active = sortKey && sort?.key === sortKey;
      return (
        <th className={`text-left px-5 py-3 font-medium whitespace-nowrap select-none ${thc}`} style={{ color: active ? "var(--text-2)" : "var(--muted)" }}>
          {sortKey ? (
            <button type="button" onClick={() => toggleSort(sortKey)} className="inline-flex items-center gap-1 focus-ring rounded hover:text-[var(--text)] transition-colors uppercase tracking-[0.06em] text-[11px]" style={{ color: "inherit" }}>
              {label}
              <SortIcon k={sortKey} />
            </button>
          ) : label}
        </th>
      );
    };

    const COLSPAN = 18;

    return (
      <div className={`${className ?? ""} ${fillHeight ? "flex flex-col min-h-0" : ""}`.trim()}>
        {/* Summary strip */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-2.5 mb-4">
          <SummaryTile label="Positions" tone="neutral"
            node={<AnimatedNumber value={summary.count} format={(n) => String(Math.round(n))} className="num" />}
            sub={`${summary.longs} long · ${summary.shorts} short`} />
          <SummaryTile label="Market value" tone="neutral"
            node={<AnimatedNumber value={summary.mv} format={fmtUsd} className="num" />}
            sub={filter === "all" ? "All instruments" : filter === "option" ? "Options" : "Stocks"} />
          <SummaryTile label="Unrealized P&L" tone={summary.pnl > 0 ? "good" : summary.pnl < 0 ? "bad" : "neutral"}
            node={<AnimatedNumber value={summary.pnl} format={fmtSignedUsd} className="num" />}
            sub="On open positions" />
          <SummaryTile label="Long / Short" tone="neutral"
            node={<span className="num">{summary.longs} / {summary.shorts}</span>}
            sub="Direction split" />
        </div>

        {/* Toolbar: type tabs + symbol search */}
        <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            {tabBtn("option", "Options")}
            {tabBtn("stock", "Stocks")}
            {tabBtn("all", "All")}
          </div>
          <div className="relative">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: "var(--muted)" }} />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search symbol…"
              className="pl-8 pr-8 py-1.5 text-sm w-44 sm:w-56"
              aria-label="Search positions by symbol"
            />
            {search && (
              <button type="button" onClick={() => setSearch("")} aria-label="Clear search"
                className="absolute right-2 top-1/2 -translate-y-1/2 focus-ring rounded" style={{ color: "var(--muted)" }}>
                <X size={14} />
              </button>
            )}
          </div>
        </div>

        <div className={`card overflow-hidden ${fillHeight ? "flex flex-col flex-1 min-h-0" : ""}`.trim()} style={{ borderRadius: 10 }}>
          <div className={`overflow-auto ${fillHeight ? "flex-1 min-h-0" : ""}`.trim()}>
            <table className={`min-w-full text-sm ${!loading && visible.length === 0 ? "h-full" : ""}`}>
              <thead className="sticky top-0 z-10" style={{ background: "var(--panel)", boxShadow: "0 1px 0 var(--border)" }}>
                <tr>
                  <Th label="Symbol" sortKey="symbol" />
                  <Th label="Expiry Date" />
                  <Th label="Type" />
                  <Th label="Side" />
                  <Th label="Quantity" sortKey="quantity" />
                  <Th label="Close %" />
                  <Th label="Actions" />
                  <Th label="Avg entry" sortKey="avg_entry_price" />
                  <Th label="Current price" sortKey="current_price" />
                  <Th label="Filled price" />
                  <Th label="TP" />
                  <Th label="SL" />
                  <Th label="Market value" sortKey="market_value" />
                  <Th label="Unrealized P&L" sortKey="unrealized_pnl" />
                  <Th label="Submitted at" />
                  <Th label="Filled at" />
                  <Th label="Time Taken to Filled" />
                  <Th label="Expires in Days" sortKey="expires" />
                </tr>
              </thead>
              <tbody>
                {loading && Array.from({ length: 5 }).map((_, i) => (
                  <tr key={`sk-${i}`} className="border-t" style={{ borderColor: "var(--border)" }}>
                    {Array.from({ length: COLSPAN }).map((__, j) => (
                      <td key={j} className="px-5 py-3.5"><div className="skeleton h-4 w-full" style={{ minWidth: 48 }} /></td>
                    ))}
                  </tr>
                ))}
                {!loading && visible.length === 0 && (
                  <tr>
                    <td colSpan={COLSPAN} className="px-3 align-middle text-center">
                      <div className="flex flex-col items-center justify-center text-center gap-2 min-h-[240px]" style={{ color: "var(--muted)" }}>
                        <Layers size={28} />
                        <div className="text-sm" style={{ color: "var(--text)" }}>
                          {positions.length === 0
                            ? "No open positions"
                            : search
                            ? `No positions match “${search}”`
                            : filter === "option" ? "No open option positions"
                            : "No open stock positions"}
                        </div>
                        <div className="text-xs">Positions appear here once your orders fill.</div>
                      </div>
                    </td>
                  </tr>
                )}
                {!loading && visible.map(p => {
                  const key = posKey(p);
                  const qtyNum = Number(p.quantity);
                  const isLong = qtyNum > 0;
                  const pnl = fmtSignedMoney(p.unrealized_pnl);
                  const inFlight = closing?.key === key;
                  return (
                    <Fragment key={key}>
                      <tr className="border-t transition-colors hover:bg-[var(--panel-2)]" style={{ borderColor: "var(--border)" }}>
                        <td className="px-5 py-3.5 font-semibold" style={{ color: "var(--text)" }}>{p.symbol}</td>
                        {/* Expiry Date — absolute date for options, "—" for stocks. */}
                        <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: p.option_expiry ? "var(--text-2)" : "var(--faint)" }}>
                          {p.option_expiry ? fmtDate(p.option_expiry) : "—"}
                        </td>
                        <td className="px-5 py-3.5">
                          <span className="chip capitalize">{p.instrument_type}</span>
                        </td>
                        <td className="px-5 py-3.5">
                          <span
                            className="chip uppercase font-semibold"
                            style={{
                              background: isLong ? "var(--good-soft)" : "var(--bad-soft)",
                              color: isLong ? "var(--good)" : "var(--bad)",
                              borderColor: "transparent",
                            }}
                          >
                            {isLong ? "Long" : "Short"}
                          </span>
                        </td>
                        <td className="px-5 py-3.5 num">{fmtNum(String(Math.abs(qtyNum)), 0)}</td>
                        {/* Close % — pick a fraction of the position to close.
                            Pills that would round to zero (e.g. 25% of one
                            contract) are disabled. */}
                        <td className="px-5 py-3.5">
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
                                    background: selected ? "var(--nav-active-bg)" : "transparent",
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
                        <td className="px-5 py-3.5">
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
                        <td className="px-5 py-3.5 num">{fmtNum(p.avg_entry_price, 2)}</td>
                        <td className="px-5 py-3.5 num">{fmtNum(p.current_price, 2)}</td>
                        {(() => {
                          const t = orderTimestamps.byKey.get(orderTimestamps.key(
                            p.broker_account_id, p.instrument_type, p.symbol,
                            p.option_expiry, p.option_strike, p.option_right,
                          ));
                          // Bracket modify is allowed while the underlying
                          // position is still alive — exactly when this row
                          // exists. Without an entry-order match (e.g. broker
                          // connected after the trade) we can't target a
                          // parent → render the cells read-only.
                          const orderId = t?.order_id ?? null;
                          // % anchor — must match the Order History logic so the
                          // two views agree. A copied mirror (parent_order_id set)
                          // re-anchors its exits on the subscriber's actual fill,
                          // so reverse the % off filled_avg_price; the trader's own
                          // entry reverses off limit_price (what the Trade Panel
                          // used to set the bracket).
                          const entryPrice = t?.parent_order_id
                            ? (t?.filled_avg_price ?? t?.limit_price ?? null)
                            : (t?.limit_price ?? t?.filled_avg_price ?? null);
                          // Copied mirror → show the trader's intended percent
                          // verbatim, not the percent re-derived from the
                          // tick-rounded exit price.
                          const isMirror = !!t?.parent_order_id;
                          const tpPct = isMirror && t?.take_profit_pct != null ? Number(t.take_profit_pct) : null;
                          const slPct = isMirror && t?.stop_loss_pct != null ? Number(t.stop_loss_pct) : null;
                          const side = t?.side ?? (isLong ? "buy" : "sell");
                          const onUpdated = (updated: Order) => {
                            setOrders(cur => cur.map(o => o.id === updated.id ? updated : o));
                          };
                          return (
                            <>
                              <td className="px-5 py-3.5 num">
                                {t?.filled_avg_price ? fmtNum(t.filled_avg_price, 2) : <span style={{ color: "var(--faint)" }}>—</span>}
                              </td>
                              <td className="px-5 py-3.5 num">
                                <InlineBracketCell
                                  orderId={orderId}
                                  leg="tp"
                                  value={t?.take_profit_price ?? null}
                                  entryPrice={entryPrice}
                                  side={side}
                                  canEdit={!!orderId}
                                  pctOverride={tpPct}
                                  onUpdated={onUpdated}
                                />
                              </td>
                              <td className="px-5 py-3.5 num">
                                <InlineBracketCell
                                  orderId={orderId}
                                  leg="sl"
                                  value={t?.stop_loss_price ?? null}
                                  entryPrice={entryPrice}
                                  side={side}
                                  canEdit={!!orderId}
                                  pctOverride={slPct}
                                  onUpdated={onUpdated}
                                />
                              </td>
                            </>
                          );
                        })()}
                        <td className="px-5 py-3.5 num">{fmtNum(p.market_value, 2)}</td>
                        <td className="px-5 py-3.5 num font-medium">
                          <span className="inline-flex items-center gap-1" style={{ color: pnl.sign === 1 ? "var(--good)" : pnl.sign === -1 ? "var(--bad)" : "var(--muted)" }}>
                            {pnl.sign === 1 && <TrendingUp size={13} />}
                            {pnl.sign === -1 && <TrendingDown size={13} />}
                            {pnl.text}
                          </span>
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
                              <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: "var(--muted)" }}>
                                {sub ? fmtDateTimeMs(sub, "America/New_York") : <span style={{ color: "var(--faint)" }}>—</span>}
                              </td>
                              <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: "var(--muted)" }}>
                                {fill ? fmtDateTimeMs(fill, "America/New_York") : <span style={{ color: "var(--faint)" }}>—</span>}
                              </td>
                              <td className="px-5 py-3.5 whitespace-nowrap num" style={{ color: fill && sub ? "var(--text-2)" : "var(--faint)" }}>
                                {sub && fill ? fmtDuration(sub, fill) : "—"}
                              </td>
                            </>
                          );
                        })()}
                        {(() => {
                          const exp = p.instrument_type === "option" ? fmtExpiresIn(p.option_expiry) : null;
                          return (
                            <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: exp ? exp.color : "var(--faint)" }}>
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
      </div>
    );
  },
);

function SummaryTile({
  label,
  node,
  sub,
  tone,
}: {
  label: string;
  node: React.ReactNode;
  sub?: string;
  tone: "neutral" | "good" | "bad";
}) {
  const color = tone === "good" ? "var(--good)" : tone === "bad" ? "var(--bad)" : "var(--text)";
  void sub; // subtitle dropped — cards match the Order History summary size
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
      className="card px-3.5 py-2.5 flex flex-col gap-4"
      style={{ borderRadius: 10 }}
    >
      <span className="text-[10px] font-medium uppercase tracking-wider truncate" style={{ color: "var(--muted)" }}>{label}</span>
      <div className="text-[19px] font-semibold leading-none tabular-nums" style={{ color }}>{node}</div>
    </motion.div>
  );
}
