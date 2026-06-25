"use client";

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { motion } from "framer-motion";
import { useSearchParams } from "next/navigation";
import { ArrowDown, ArrowUp, ChevronsUpDown, Inbox, Search, X } from "lucide-react";
import { api } from "@/lib/api";
import { fmtDate, fmtDateTimeMs, fmtDuration, fmtUsd } from "@/lib/format";
import { useEventStream } from "@/lib/sse";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { AnimatedNumber } from "@/components/dashboard/AnimatedNumber";
import { InlineBracketCell } from "@/components/InlineBracketCell";
import type { Order, OrderStatus, Position, TradeStats, User } from "@/lib/types";

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

/** Latest fill timestamp for an order (or closed_at fallback for filled). */
function lastFillTs(o: Order): string | null {
  const lastFillAt = o.fills?.length
    ? o.fills.reduce((a, b) => (a.filled_at > b.filled_at ? a : b)).filled_at
    : null;
  return lastFillAt ?? (o.status === "filled" ? o.closed_at : null);
}

/** Option expiry rendered as a relative day count ("in 2 days", "Today",
 *  "Expired 3d ago"). UTC-anchored so timezone offsets don't tip the count. */
function expiresDays(isoDate: string | null): number | null {
  if (!isoDate) return null;
  const target = new Date(isoDate + (isoDate.length === 10 ? "T00:00:00Z" : ""));
  if (Number.isNaN(target.getTime())) return null;
  const now = new Date();
  const t0 = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const t1 = Date.UTC(target.getUTCFullYear(), target.getUTCMonth(), target.getUTCDate());
  return Math.round((t1 - t0) / 86_400_000);
}

function fmtExpiresIn(isoDate: string | null): { text: string; color: string } | null {
  const d = expiresDays(isoDate);
  if (d === null || !Number.isFinite(d)) return null;
  // Past expiries collapse to "Expired" (in red); "Today" reads better
  // than "0"; otherwise show the raw day count.
  if (d < 0) return { text: "Expired", color: "var(--bad)" };
  if (d === 0) return { text: "Today", color: "var(--bad)" };
  if (d === 1) return { text: String(d), color: "var(--bad)" };
  return { text: String(d), color: "var(--text)" };
}

const STATUS_STYLE: Record<string, { bg: string; color: string }> = {
  filled: { bg: "var(--good-soft)", color: "var(--good)" },
  rejected: { bg: "var(--bad-soft)", color: "var(--bad)" },
  canceled: { bg: "var(--panel-2)", color: "var(--muted)" },
  expired: { bg: "var(--panel-2)", color: "var(--muted)" },
  retry_pending: { bg: "rgba(250,204,21,0.12)", color: "var(--warn)" },
};
const STATUS_DEFAULT = { bg: "var(--accent-glow)", color: "var(--accent)" };

// ── Sorting ─────────────────────────────────────────────────────────────────
type SortKey = "symbol" | "quantity" | "notional" | "status" | "submitted" | "filled" | "expires";

function sortValue(o: Order, key: SortKey): number | string {
  switch (key) {
    case "symbol": return o.symbol.toUpperCase();
    case "quantity": return Number(o.quantity) || 0;
    case "notional": return notionalFor(o);
    case "status": return o.status;
    case "submitted": return new Date(o.submitted_at ?? o.created_at).getTime() || 0;
    case "filled": { const t = lastFillTs(o); return t ? new Date(t).getTime() : 0; }
    case "expires": { const d = expiresDays(o.option_expiry); return d === null ? Number.POSITIVE_INFINITY : d; }
  }
}

// How many orders to load into the Order History window. Matches the
// backend's hard cap (GET /api/trades — limit le=1000), so the tab count
// stays accurate up to 1000 orders instead of pinning at the old default
// of 200 (which made a freshly-placed order briefly show 201 then snap
// back to 200 once the reconcile refetch trimmed the window).
const PAGE_LIMIT = 1000;

export default function TradesPage() {
  const searchParams = useSearchParams();
  // Optional ?from=YYYY-MM-DD&to=YYYY-MM-DD filter (used by Calendar drill-in).
  const fromParam = searchParams?.get("from") ?? null;
  const toParam = searchParams?.get("to") ?? null;

  const [orders, setOrders] = useState<Order[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  // DB-computed order totals (GET /api/trades/stats) — drives the summary
  // tiles + tab badges so they reflect EVERY matching order, not just the
  // fetched window. null until first load (we fall back to local counts).
  const [stats, setStats] = useState<TradeStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [flashId, setFlashId] = useState<string | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [tab, setTab] = useState<"all" | "mine">("all");

  // Presentational only.
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" } | null>(null);

  const [actingFor, setActingFor] = useState<{ id: string; kind: "cancel" | "market" | "limit" } | null>(null);
  const [closePrices, setClosePrices] = useState<Record<string, string>>({});
  const reconcileTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Single source of truth for the trades URL — used by both the initial
  // load and the post-event reconcile so they fetch the SAME window
  // (limit + any date filter). Previously the reconcile hit a bare
  // "/api/trades", which both capped at 200 and dropped the from/to filter.
  const tradesEndpoint = useCallback(() => {
    const q = new URLSearchParams();
    q.set("limit", String(PAGE_LIMIT));
    if (fromParam) q.set("from", fromParam);
    if (toParam) {
      const t = new Date(toParam + "T00:00:00Z");
      t.setUTCDate(t.getUTCDate() + 1);
      q.set("to", t.toISOString().slice(0, 10));
    }
    return `/api/trades?${q.toString()}`;
  }, [fromParam, toParam]);

  // DB-aggregate totals, same date filter as the list. Fetched alongside
  // the rows and refreshed whenever orders change (SSE / reconcile) so the
  // summary stays live without being bound to the fetched page size.
  const loadStats = useCallback(async () => {
    const q = new URLSearchParams();
    if (fromParam) q.set("from", fromParam);
    if (toParam) {
      const t = new Date(toParam + "T00:00:00Z");
      t.setUTCDate(t.getUTCDate() + 1);
      q.set("to", t.toISOString().slice(0, 10));
    }
    const suffix = q.toString();
    try {
      const s = await api<TradeStats>(`/api/trades/stats${suffix ? `?${suffix}` : ""}`);
      setStats(s);
    } catch { /* tolerate — summary falls back to local counts */ }
  }, [fromParam, toParam]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Fetch the user FIRST (fast) so the All/My tabs render immediately
      // while the slower sync-fills + order fetch run behind the table
      // skeleton. Previously this was bundled into the Promise.all below,
      // so the tabs only appeared once the (slow) order load finished.
      try {
        const u = await api<User>("/api/auth/me");
        if (!cancelled) setUser(u);
      } catch { /* tolerate — tabs are trader-only, fall back to none */ }

      try { await api("/api/trades/sync-fills", { method: "POST" }); } catch { /* non-blocking */ }
      if (cancelled) return;
      const [o, p] = await Promise.all([
        api<Order[]>(tradesEndpoint()),
        api<Position[]>("/api/positions").catch(() => [] as Position[]),
        loadStats(),
      ]);
      if (!cancelled) {
        setOrders(o);
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
      evt.type !== "order.copy_retry_scheduled" &&
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
        take_profit_price: idx >= 0 ? cur[idx].take_profit_price : null,
        stop_loss_price: idx >= 0 ? cur[idx].stop_loss_price : null,
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
        // Brand-new order: prepend, but keep the list within the same
        // window the reconcile refetch will settle on, so the count
        // doesn't briefly overshoot (e.g. 200 → 201 → 200).
        : [merged, ...cur].slice(0, PAGE_LIMIT);
      return next;
    });
    setFlashId(incoming.id);
    setTimeout(() => setFlashId((f) => (f === incoming.id ? null : f)), 2000);

    // Counts changed (new order, or a status transition) — refresh the
    // DB totals. Fire-and-forget; cheap aggregate query.
    loadStats();

    const terminal = incoming.status === "filled" || incoming.status === "canceled" || incoming.status === "rejected";
    if (!terminal) {
      if (reconcileTimer.current) clearTimeout(reconcileTimer.current);
      reconcileTimer.current = setTimeout(async () => {
        try { await api("/api/trades/sync-fills", { method: "POST" }); } catch { /* ignore */ }
        try {
          const fresh = await api<Order[]>(tradesEndpoint());
          setOrders(fresh);
          loadStats();
        } catch { /* ignore */ }
      }, 1500);
    }
  });

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
      api<Position[]>("/api/positions").then(setPositions).catch(() => {});
    } catch (e) {
      notify.fromError(e, "close failed");
    } finally {
      setActingFor(null);
    }
  }

  // Held-position keys + tab/date filter — hoisted to a memo so the summary
  // strip and the table body share one source of truth (logic unchanged).
  const visibleOrders = useMemo(() => {
    const normStrike = (s: string | null) => {
      if (s == null) return "";
      const n = Number(s);
      return Number.isFinite(n) ? String(n) : s;
    };
    const normExpiry = (s: string | null) => (s ?? "").slice(0, 10);
    const posKey = (
      acctId: string, instrument: string, symbol: string,
      expiry: string | null, strike: string | null, right: string | null,
    ) =>
      instrument === "option"
        ? `${acctId}:OPT:${symbol.toUpperCase()}:${normExpiry(expiry)}:${normStrike(strike)}:${right ?? ""}`
        : `${acctId}:STK:${symbol.toUpperCase()}`;

    const heldKeys = new Set(
      positions
        .filter(p => Number(p.quantity) !== 0)
        .map(p => posKey(p.broker_account_id, p.instrument_type, p.symbol, p.option_expiry, p.option_strike, p.option_right))
    );

    // "All Orders" = every order (no filter — a superset). "My Orders"
    // (trader only) = the subset placed while copy was OFF. So a copy-off
    // order appears under BOTH tabs; a copy-on order only under All.
    return orders.filter(o => {
      if (user?.role === "trader" && tab === "mine" && o.fanned_out_to_subscribers) return false;
      // Bracket exit legs (TP/SL) are auto-placed protective orders for a
      // position, not trades the user placed — the bracket already shows as
      // the position's / entry's TP/SL columns. Surface a leg ONLY when it
      // actually filled (the real close); hide resting, cancelled, and
      // rejected legs so they don't clutter history as phantom "not filled"
      // rows sitting next to the open position they protect.
      if (o.bracket_parent_id && o.status !== "filled") return false;
      if (fromParam || toParam) return true;
      if (o.status !== "filled") return true;
      return !heldKeys.has(posKey(
        o.broker_account_id ?? "", o.instrument_type, o.symbol,
        o.option_expiry, o.option_strike, o.option_right,
      ));
    });
  }, [orders, positions, tab, fromParam, toParam, user]);

  // search → sort (presentational)
  const rows = useMemo(() => {
    const q = search.trim().toUpperCase();
    const bySearch = q ? visibleOrders.filter(o => o.symbol.toUpperCase().includes(q)) : visibleOrders;
    if (!sort) return bySearch;
    const arr = [...bySearch];
    arr.sort((a, b) => {
      const va = sortValue(a, sort.key);
      const vb = sortValue(b, sort.key);
      const cmp = typeof va === "string" ? va.localeCompare(vb as string) : (va as number) - (vb as number);
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [visibleOrders, search, sort]);

  const summary = useMemo(() => {
    let filled = 0, working = 0, notional = 0;
    for (const o of visibleOrders) {
      if (o.status === "filled") filled++;
      if (OPEN_STATUSES.includes(o.status)) working++;
      notional += notionalFor(o);
    }
    return { total: visibleOrders.length, filled, working, notional };
  }, [visibleOrders]);

  // Prefer DB-computed totals (every matching order); fall back to the
  // local page-derived counts only until the first stats response lands.
  const scopeStats = stats ? (tab === "mine" ? stats.mine : stats.all) : null;
  const view = {
    total: scopeStats ? scopeStats.total : summary.total,
    filled: scopeStats ? scopeStats.filled : summary.filled,
    working: scopeStats ? scopeStats.working : summary.working,
    notional: scopeStats ? Number(scopeStats.notional) : summary.notional,
  };

  function toggleSort(key: SortKey) {
    setSort(prev => {
      if (!prev || prev.key !== key) return { key, dir: "asc" };
      if (prev.dir === "asc") return { key, dir: "desc" };
      return null;
    });
  }

  const SortIcon = ({ k }: { k: SortKey }) => {
    if (!sort || sort.key !== k) return <ChevronsUpDown size={12} style={{ opacity: 0.4 }} />;
    return sort.dir === "asc" ? <ArrowUp size={12} /> : <ArrowDown size={12} />;
  };

  const Th = ({ label, sortKey }: { label: string; sortKey?: SortKey }) => {
    const active = sortKey && sort?.key === sortKey;
    return (
      <th className="text-left px-5 py-3 font-medium whitespace-nowrap select-none" style={{ color: active ? "var(--text-2)" : "var(--muted)" }}>
        {sortKey ? (
          <button type="button" onClick={() => toggleSort(sortKey)} className="inline-flex items-center gap-1 focus-ring rounded hover:text-[var(--text)] transition-colors uppercase tracking-[0.06em] text-[11px]">
            {label}<SortIcon k={sortKey} />
          </button>
        ) : label}
      </th>
    );
  };

  const COLSPAN = 16;

  return (
    <div className="flex flex-col h-full min-h-0">
      {(fromParam || toParam) && (
        <div
          className="flex items-center justify-between gap-3 px-4 py-2.5 rounded-token mb-4 text-sm"
          style={{ border: "1px solid var(--border)", background: "var(--accent-glow)" }}
        >
          <div style={{ color: "var(--text-2)" }}>
            {"Showing trades for "}
            <strong>{fromParam === toParam || !toParam ? fromParam : `${fromParam} → ${toParam}`}</strong>
          </div>
          <Link href="/trades" prefetch={false} className="text-xs no-underline focus-ring rounded" style={{ color: "var(--accent)" }}>
            Clear filter
          </Link>
        </div>
      )}

      {/* Summary strip */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-2.5 mb-4">
        <SummaryTile label="Total orders" node={<AnimatedNumber value={view.total} format={(n) => String(Math.round(n))} className="num" />} />
        <SummaryTile label="Filled" tone="good" node={<AnimatedNumber value={view.filled} format={(n) => String(Math.round(n))} className="num" />} />
        <SummaryTile label="Working" tone="accent" node={<AnimatedNumber value={view.working} format={(n) => String(Math.round(n))} className="num" />} />
        <SummaryTile label="Filled notional" node={<AnimatedNumber value={view.notional} format={fmtUsd} className="num" />} />
      </div>

      {/* Toolbar: tabs (trader) + symbol search */}
      <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
        <div className="flex gap-2 items-center">
          {user?.role === "trader" && (() => {
            // Prefer DB totals so the badges reflect every order, not just
            // the fetched window. Fall back to local counts pre-load.
            // Counts come from the DB stats; null until they load so we
            // show the tab without a misleading "(0)" during the fetch.
            const mineCount = stats ? stats.mine.total : null;
            const allCount = stats ? stats.all.total : null;
            const Tab = ({ k, label, count }: { k: "mine" | "all"; label: string; count: number | null }) => {
              const active = tab === k;
              return (
                <button
                  key={k}
                  type="button"
                  onClick={() => setTab(k)}
                  className="px-3 py-1.5 text-xs font-medium rounded-full transition-colors focus-ring"
                  style={{
                    border: `1px solid ${active ? "rgba(10,115,168,0.4)" : "var(--border)"}`,
                    background: active ? "var(--nav-active-bg)" : "transparent",
                    color: active ? "var(--accent)" : "var(--text-2)",
                  }}
                >
                  {label}
                  {count != null && (
                    <span style={{ color: active ? "var(--accent)" : "var(--muted)" }}> ({count})</span>
                  )}
                </button>
              );
            };
            return (<><Tab k="all" label="All Orders" count={allCount} /><Tab k="mine" label="My Orders" count={mineCount} /></>);
          })()}
        </div>
        <div className="relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: "var(--muted)" }} />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search symbol…"
            className="pl-8 pr-8 py-1.5 text-sm w-44 sm:w-56"
            aria-label="Search orders by symbol"
          />
          {search && (
            <button type="button" onClick={() => setSearch("")} aria-label="Clear search"
              className="absolute right-2 top-1/2 -translate-y-1/2 focus-ring rounded" style={{ color: "var(--muted)" }}>
              <X size={14} />
            </button>
          )}
        </div>
      </div>

      <div className="card overflow-hidden flex flex-col flex-1 min-h-0" style={{ borderRadius: 10 }}>
        <div className="overflow-auto flex-1 min-h-0">
          <table className={`min-w-full text-sm ${!loading && rows.length === 0 ? "h-full" : ""}`}>
            <thead className="sticky top-0 z-10" style={{ background: "var(--panel)", boxShadow: "0 1px 0 var(--border)" }}>
              <tr>
                <Th label="Symbol" sortKey="symbol" />
                <Th label="Expiry Date" />
                <Th label="Type" />
                <Th label="Side" />
                <Th label="Quantity" sortKey="quantity" />
                <Th label="Actions" />
                <Th label="Expected price" />
                <Th label="Filled price" />
                <Th label="TP" />
                <Th label="SL" />
                <Th label="Notional" sortKey="notional" />
                <Th label="Status" sortKey="status" />
                <Th label="Submitted at" sortKey="submitted" />
                <Th label="Filled at" sortKey="filled" />
                <Th label="Time Taken to Filled" />
                <Th label="Expires in Days" sortKey="expires" />
              </tr>
            </thead>
            <tbody>
              {loading && Array.from({ length: 6 }).map((_, i) => (
                <tr key={`sk-${i}`} className="border-t" style={{ borderColor: "var(--border)" }}>
                  {Array.from({ length: COLSPAN }).map((__, j) => (
                    <td key={j} className="px-5 py-3.5"><div className="skeleton h-4 w-full" style={{ minWidth: 44 }} /></td>
                  ))}
                </tr>
              ))}
              {!loading && rows.length === 0 && (
                <tr>
                  <td colSpan={COLSPAN} className="px-3 align-middle text-center">
                    <div className="flex flex-col items-center justify-center text-center gap-2 min-h-[240px]" style={{ color: "var(--muted)" }}>
                      <Inbox size={28} />
                      <div className="text-sm" style={{ color: "var(--text)" }}>
                        {orders.length === 0 ? "No trades yet" : search ? `No orders match “${search}”` : "No orders in this view"}
                      </div>
                      <div className="text-xs">Orders appear here as you place or mirror them.</div>
                    </div>
                  </td>
                </tr>
              )}
              {!loading && rows.map(o => {
                const isOpen = OPEN_STATUSES.includes(o.status);
                const canCancel = isOpen;
                // No more Close buttons in Order History — close lives on the
                // Trade Panel's Open Positions table now.
                const canClose = false;
                const buy = o.side === "buy";
                const st = STATUS_STYLE[o.status] ?? STATUS_DEFAULT;
                const fillTs = lastFillTs(o);
                const submittedTs = o.submitted_at ?? o.created_at;
                const exp = o.instrument_type === "option" ? fmtExpiresIn(o.option_expiry) : null;
                return (
                  <Fragment key={o.id}>
                    <tr
                      className="border-t transition-colors hover:bg-[var(--panel-2)]"
                      style={{
                        borderColor: "var(--border)",
                        background: flashId === o.id ? "var(--good-soft)" : undefined,
                      }}
                    >
                      <td className="px-5 py-3.5 font-semibold" style={{ color: "var(--text)" }}>{o.symbol}</td>
                      <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: o.option_expiry ? "var(--text-2)" : "var(--faint)" }}>
                        {o.option_expiry ? fmtDate(o.option_expiry) : "—"}
                      </td>
                      <td className="px-5 py-3.5"><span className="chip capitalize">{o.instrument_type}</span></td>
                      <td className="px-5 py-3.5">
                        <span className="chip uppercase font-semibold" style={{ background: buy ? "var(--good-soft)" : "var(--bad-soft)", color: buy ? "var(--good)" : "var(--bad)", borderColor: "transparent" }}>
                          {o.side}
                        </span>
                      </td>
                      <td className="px-5 py-3.5 num">{fmt(o.quantity, 0)}</td>
                      <td className="px-5 py-3.5">
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
                              <div className="flex items-stretch">
                                <input
                                  type="number" step="0.01" min="0.01"
                                  placeholder="Limit"
                                  value={closePrices[o.id] ?? ""}
                                  onChange={e => setClosePrices(p => ({ ...p, [o.id]: e.target.value }))}
                                  className="w-20 px-2 py-1 text-xs"
                                  style={{
                                    borderTopLeftRadius: "var(--r-sm)", borderBottomLeftRadius: "var(--r-sm)",
                                    borderTopRightRadius: 0, borderBottomRightRadius: 0, borderRight: "none",
                                  }}
                                />
                                <button
                                  disabled={actingFor?.id === o.id || !closePrices[o.id]}
                                  onClick={() => closeAt(o.id, "limit")}
                                  className="btn-accent-solid px-3 py-1 text-xs font-medium inline-flex items-center gap-1.5"
                                  style={{
                                    borderTopLeftRadius: 0, borderBottomLeftRadius: 0,
                                    borderTopRightRadius: "var(--r-sm)", borderBottomRightRadius: "var(--r-sm)",
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
                      <td className="px-5 py-3.5 num">{fmt(expectedPrice(o), 2)}</td>
                      <td className="px-5 py-3.5 num">{fmt(o.filled_avg_price, 2)}</td>
                      {/* TP / SL — shown as a percent of the entry-side price.
                          Editable only on entry rows that are still open
                          (pre-fill); filled orders that survive here belong to
                          positions that already closed, so brackets are
                          immutable. Bracket-exit legs (TP/SL closes) never
                          expose an editor. Anchor the % off limit_price (the
                          exact number the Trade Panel used to set the bracket),
                          falling back to filled_avg_price for market entries.
                          For a COPIED mirror (parent_order_id set) the exits are
                          re-anchored on the subscriber's actual fill, so display
                          the % off filled_avg_price to match what fires. */}
                      {(() => {
                        const isEntry = !o.bracket_parent_id;
                        const editable = isEntry && isOpen;
                        const entryPrice = o.parent_order_id
                          ? (o.filled_avg_price ?? o.limit_price)
                          : (o.limit_price ?? o.filled_avg_price);
                        const onUpdated = (updated: Order) =>
                          setOrders(cur => cur.map(x => x.id === updated.id ? updated : x));
                        return (
                          <>
                            <td className="px-5 py-3.5 num">
                              {isEntry ? (
                                <InlineBracketCell
                                  orderId={o.id}
                                  leg="tp"
                                  value={o.take_profit_price}
                                  entryPrice={entryPrice}
                                  side={o.side}
                                  canEdit={editable}
                                  onUpdated={onUpdated}
                                />
                              ) : <span style={{ color: "var(--faint)" }}>—</span>}
                            </td>
                            <td className="px-5 py-3.5 num">
                              {isEntry ? (
                                <InlineBracketCell
                                  orderId={o.id}
                                  leg="sl"
                                  value={o.stop_loss_price}
                                  entryPrice={entryPrice}
                                  side={o.side}
                                  canEdit={editable}
                                  onUpdated={onUpdated}
                                />
                              ) : <span style={{ color: "var(--faint)" }}>—</span>}
                            </td>
                          </>
                        );
                      })()}
                      <td className="px-5 py-3.5 num">
                        {notionalFor(o) ? fmt(String(notionalFor(o))) : <span style={{ color: "var(--faint)" }}>—</span>}
                      </td>
                      <td className="px-5 py-3.5">
                        <span
                          className="chip uppercase tracking-wider font-medium whitespace-nowrap"
                          style={{ background: st.bg, color: st.color, borderColor: "transparent" }}
                        >
                          {o.status}{o.parent_order_id ? " · copy" : ""}
                        </span>
                      </td>
                      <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: "var(--muted)" }}>
                        {fmtDateTimeMs(submittedTs, "America/New_York")}
                      </td>
                      <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: "var(--muted)" }}>
                        {fillTs ? fmtDateTimeMs(fillTs, "America/New_York") : <span style={{ color: "var(--faint)" }}>—</span>}
                      </td>
                      <td className="px-5 py-3.5 whitespace-nowrap num" style={{ color: fillTs ? "var(--text-2)" : "var(--faint)" }}>
                        {fillTs ? fmtDuration(submittedTs, fillTs) : "—"}
                      </td>
                      <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: exp ? exp.color : "var(--faint)" }}>
                        {exp ? exp.text : "—"}
                      </td>
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
}

function SummaryTile({
  label,
  node,
  tone = "neutral",
}: {
  label: string;
  node: React.ReactNode;
  tone?: "neutral" | "good" | "accent";
}) {
  const color = tone === "good" ? "var(--good)" : tone === "accent" ? "var(--accent)" : "var(--text)";
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
