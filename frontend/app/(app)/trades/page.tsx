"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { motion } from "framer-motion";
import { useSearchParams } from "next/navigation";
import { ArrowDown, ArrowUp, ChevronsUpDown, Inbox, Search, X } from "lucide-react";
import { api } from "@/lib/api";
import { ExportButton } from "@/components/ExportButton";
import { PositionIcon, orderKind } from "@/components/PositionIcon";
import { fmtDateTimeMs, fmtDuration, fmtUsd } from "@/lib/format";
import { useEventStream } from "@/lib/sse";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { AnimatedNumber } from "@/components/dashboard/AnimatedNumber";
import { InlineBracketCell } from "@/components/InlineBracketCell";
import Pagination from "@/components/Pagination";
import type { Order, OrderStatus, Position, TradeStats, User } from "@/lib/types";

const OPEN_STATUSES: OrderStatus[] = ["pending", "submitted", "accepted", "partially_filled"];

// Webull-style status tabs. Filtering is server-side now (trade_filters); the
// tab key is passed straight to /api/trades/page as ?status=.
type StatusTab = "all" | "working" | "filled" | "cancelled" | "rejected";
const STATUS_TABS: { key: StatusTab; label: string }[] = [
  { key: "all", label: "All" },
  { key: "working", label: "Working" },
  { key: "filled", label: "Filled" },
  { key: "cancelled", label: "Cancelled" },
  { key: "rejected", label: "Rejected" },
];

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

/** Short option expiry, matching the Positions table ("10 Jul 26"). */
function optionExpiryShort(isoDate: string): string {
  const d = new Date(isoDate.length === 10 ? isoDate + "T00:00:00Z" : isoDate);
  if (Number.isNaN(d.getTime())) return isoDate;
  const mon = d.toLocaleDateString("en-US", { month: "short", timeZone: "UTC" });
  return `${d.getUTCDate()} ${mon} ${String(d.getUTCFullYear()).slice(-2)}`;
}

/** Full contract descriptor for the Symbol column — same style as the Positions
 *  table: stock → "META"; option → "META C $372 10 Jul 26". Folds in call/put,
 *  strike and expiry so no separate columns are needed. */
function orderSymbolLabel(o: Order): string {
  if (o.instrument_type !== "option") return o.symbol.toUpperCase();
  const cp = o.option_right === "call" ? "C" : o.option_right === "put" ? "P" : "";
  const strike = o.option_strike != null && o.option_strike !== ""
    ? `$${Number(o.option_strike)}` : "";
  const exp = o.option_expiry ? optionExpiryShort(o.option_expiry) : "";
  return [o.symbol.toUpperCase(), cp, strike, exp].filter(Boolean).join(" ");
}

/** "Expected" price the user asked for: the limit (or stop) price they set,
 *  or null for market orders. */
function expectedPrice(o: Order): string | null {
  if (o.order_type === "limit" || o.order_type === "stop_limit") return o.limit_price;
  if (o.order_type === "stop") return o.stop_price;
  return null;
}

/** Human label for the order type shown in the Order History table. */
function orderTypeLabel(t: Order["order_type"]): string {
  switch (t) {
    case "market": return "Market";
    case "limit": return "Limit";
    case "stop": return "Stop";
    case "stop_limit": return "Stop Limit";
    default: return t;
  }
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
  const [tab, setTab] = useState<StatusTab>("all");

  const [search, setSearch] = useState("");
  // Debounced copy of `search` — drives the server refetch so we don't hit the
  // API on every keystroke. `search` stays bound to the input for instant echo.
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" } | null>(null);

  // Server-side pagination state.
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(50);

  const [actingFor, setActingFor] = useState<{ id: string; kind: "cancel" | "market" | "limit" } | null>(null);
  const [closePrices, setClosePrices] = useState<Record<string, string>>({});
  const reconcileTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Single source of truth for the trades URL — used by both the initial
  // load and the post-event reconcile so they fetch the SAME window
  // (limit + any date filter). Previously the reconcile hit a bare
  // "/api/trades", which both capped at 200 and dropped the from/to filter.
  const tradesEndpoint = useCallback(() => {
    const q = new URLSearchParams();
    q.set("limit", String(limit));
    q.set("offset", String(offset));
    if (tab !== "all") q.set("status", tab);
    if (debouncedSearch.trim()) q.set("search", debouncedSearch.trim());
    if (sort) { q.set("sort", sort.key); q.set("dir", sort.dir); }
    if (fromParam) q.set("from", fromParam);
    if (toParam) {
      const t = new Date(toParam + "T00:00:00Z");
      t.setUTCDate(t.getUTCDate() + 1);
      q.set("to", t.toISOString().slice(0, 10));
    }
    return `/api/trades/page?${q.toString()}`;
  }, [tab, debouncedSearch, sort, limit, offset, fromParam, toParam]);

  // Fetch the current page from the server (filters/sort/paging all server-side).
  const loadPage = useCallback(async () => {
    try {
      const page = await api<{ items: Order[]; total: number }>(tradesEndpoint());
      setOrders(page.items);
      setTotal(page.total);
    } catch { /* tolerate — keep the last page on a transient error */ }
  }, [tradesEndpoint]);

  // Same date window as tradesEndpoint(), PLUS the tab and search that this
  // page normally applies in the browser — the export is built server-side, so
  // it has to be told about them or the file won't match what's on screen.
  // No `limit` on purpose: the table shows a window, the export is everything.
  const exportEndpoint = useCallback(() => {
    const q = new URLSearchParams();
    if (fromParam) q.set("from", fromParam);
    if (toParam) {
      const t = new Date(toParam + "T00:00:00Z");
      t.setUTCDate(t.getUTCDate() + 1);
      q.set("to", t.toISOString().slice(0, 10));
    }
    if (tab !== "all") q.set("status", tab);
    if (search.trim()) q.set("search", search.trim());
    return `/api/trades/export?${q.toString()}`;
  }, [fromParam, toParam, tab, search]);

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

  // Setup: user, a one-time fills sync, positions, and the DB summary. Runs on
  // mount and whenever the date filter changes — NOT on every page/tab/sort
  // change (that's the page-load effect below).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const u = await api<User>("/api/auth/me");
        if (!cancelled) setUser(u);
      } catch { /* tolerate — tabs are trader-only, fall back to none */ }
      try { await api("/api/trades/sync-fills", { method: "POST" }); } catch { /* non-blocking */ }
      if (cancelled) return;
      const p = await api<Position[]>("/api/positions").catch(() => [] as Position[]);
      if (!cancelled) setPositions(p);
      loadStats();
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromParam, toParam]);

  // Page load: refetch whenever the query (tab / search / sort / page / date)
  // changes. All filtering, sorting and paging is server-side now.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      await loadPage();
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [loadPage]);

  // Debounce the search box → server refetch, and jump back to page 1.
  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedSearch(search);
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [search]);

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
    // Flash the affected row (it'll be on the current page after the refetch,
    // if it belongs there under the active filters/sort).
    setFlashId(incoming.id);
    setTimeout(() => setFlashId((f) => (f === incoming.id ? null : f)), 2000);

    // Paging is server-side, so we don't mutate an in-memory list — we debounce
    // a refetch of the CURRENT page + the DB counts. Debouncing collapses a
    // burst of copy events (a fanout) into a single round-trip.
    if (reconcileTimer.current) clearTimeout(reconcileTimer.current);
    reconcileTimer.current = setTimeout(async () => {
      const terminal = incoming.status === "filled" || incoming.status === "canceled" || incoming.status === "rejected";
      // A still-working order may have a fill the activities feed hasn't
      // surfaced yet — nudge a sync before refetching so the row lands settled.
      if (!terminal) { try { await api("/api/trades/sync-fills", { method: "POST" }); } catch { /* ignore */ } }
      await loadPage();
      loadStats();
    }, 800);
  });

  useEffect(() => {
    return () => { if (reconcileTimer.current) clearTimeout(reconcileTimer.current); };
  }, []);

  async function cancelOrder(id: string) {
    setActingFor({ id, kind: "cancel" });
    try {
      const updated = await api<Order>(`/api/trades/${id}/cancel`, { method: "POST" });
      await loadPage();
      loadStats();
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
      await loadPage();
      loadStats();
      if (type === "limit") setClosePrices(p => ({ ...p, [id]: "" }));
      notify.success(`Close placed: ${newOrder.side.toUpperCase()} ${newOrder.symbol} (${type})`);
      api<Position[]>("/api/positions").then(setPositions).catch(() => {});
    } catch (e) {
      notify.fromError(e, "close failed");
    } finally {
      setActingFor(null);
    }
  }

  // Rows ARE the server page — bracket-leg exclusion, status tab, symbol search
  // and sort all happen server-side now (/api/trades/page + trade_filters).
  const rows = orders;

  // Tab badges + summary tiles come from the DB aggregate (every matching row,
  // not just this page), so they stay stable as you flip pages. Zeros until the
  // first stats load lands.
  const s = stats?.all ?? null;
  const tabCounts: Record<StatusTab, number> = {
    all: s ? s.total : 0,
    working: s ? s.working : 0,
    filled: s ? s.filled : 0,
    cancelled: s ? s.cancelled : 0,
    rejected: s ? s.rejected : 0,
  };
  const view = {
    total: s ? s.total : 0,
    filled: s ? s.filled : 0,
    working: s ? s.working : 0,
    notional: s ? Number(s.notional) : 0,
  };

  // Sorting is server-side; changing it jumps back to page 1 and refetches.
  function toggleSort(key: SortKey) {
    setOffset(0);
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

  const Th = ({ label, sortKey, className: thc = "" }: { label: string; sortKey?: SortKey; className?: string }) => {
    const active = sortKey && sort?.key === sortKey;
    return (
      <th className={`text-left px-5 py-3 font-medium whitespace-nowrap select-none ${thc}`} style={{ color: active ? "var(--text-2)" : "var(--muted)" }}>
        {sortKey ? (
          <button type="button" onClick={() => toggleSort(sortKey)} className="inline-flex items-center gap-1 focus-ring rounded hover:text-[var(--text)] transition-colors uppercase tracking-[0.06em] text-[11px]">
            {label}<SortIcon k={sortKey} />
          </button>
        ) : label}
      </th>
    );
  };

  const COLSPAN = 15;

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
        <div className="flex gap-2 items-center flex-wrap">
          {STATUS_TABS.map(({ key, label }) => {
            const active = tab === key;
            const count = tabCounts[key];
            return (
              <button
                key={key}
                type="button"
                onClick={() => { setTab(key); setOffset(0); }}
                className="px-3 py-1.5 text-xs font-medium rounded-full transition-colors focus-ring"
                style={{
                  border: `1px solid ${active ? "rgba(10,115,168,0.4)" : "var(--border)"}`,
                  background: active ? "var(--nav-active-bg)" : "transparent",
                  color: active ? "var(--accent)" : "var(--text-2)",
                }}
              >
                {label}
                <span style={{ color: active ? "var(--accent)" : "var(--muted)" }}> ({count})</span>
              </button>
            );
          })}
        </div>
        {/* Search + Export share a group so the toolbar's justify-between
            still puts the tabs left and this cluster right. */}
        <div className="flex items-center gap-2">
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
          {/* Exports EVERY row matching these filters, not just the loaded
              window — see /api/trades/export. */}
          <ExportButton path={exportEndpoint()} label="Export" fallbackName="kopyya-trades.xlsx" />
        </div>
      </div>

      <div className="card overflow-hidden flex flex-col flex-1 min-h-0" style={{ borderRadius: 10 }}>
        <div className="overflow-auto flex-1 min-h-0">
          <table className={`min-w-full text-sm ${!loading && rows.length === 0 ? "h-full" : ""}`}>
            <thead className="sticky top-0 z-10" style={{ background: "var(--panel)", boxShadow: "0 1px 0 var(--border)" }}>
              <tr>
                <Th label="Symbol" sortKey="symbol" />
                <Th label="Qty" sortKey="quantity" />
                <Th label="Side" />
                <Th label="Actions" />
                <Th label="Status" sortKey="status" />
                <Th label="Order Type" />
                <Th label="Expected price" />
                <Th label="Filled price" />
                <Th label="TP" />
                <Th label="SL" />
                <Th label="Notional" sortKey="notional" />
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
                        {debouncedSearch ? `No orders match “${debouncedSearch}”` : (tab !== "all" || fromParam) ? "No orders in this view" : "No trades yet"}
                      </div>
                      <div className="text-xs">Orders appear here as you place or mirror them.</div>
                    </div>
                  </td>
                </tr>
              )}
              {!loading && rows.map(o => {
                const isOpen = OPEN_STATUSES.includes(o.status);
                // Cancel is available for genuinely-working orders that aren't
                // already FULLY filled. We key off the order's own fill — NOT
                // whether we hold a position in the same symbol — so a resting
                // order stays cancellable even when a position is already open
                // in that symbol. A fully-filled order (status stale to
                // "accepted", but filled_quantity caught up) still hides Cancel.
                const orderQty = Number(o.quantity) || 0;
                const filledQty = Number(o.filled_quantity) || 0;
                const fullyFilled = orderQty > 0 && filledQty >= orderQty;
                const canCancel = isOpen && !fullyFilled;
                // No more Close buttons in Order History — close lives on the
                // Trade Panel's Open Positions table now.
                const canClose = false;
                // Event-log display: once an order has (partially) filled, its OWN
                // row is frozen to the placement state ("submitted") and the fill is
                // shown as a separate row ABOVE. The real o.status still drives the
                // Cancel button, the status tabs + counts, and sorting.
                const placementStatus = (o.status === "filled" || o.status === "partially_filled")
                  ? "submitted"
                  : o.status;
                const st = STATUS_STYLE[placementStatus] ?? STATUS_DEFAULT;
                const fillTs = lastFillTs(o);
                const submittedTs = o.submitted_at ?? o.created_at;
                const exp = o.instrument_type === "option" ? fmtExpiresIn(o.option_expiry) : null;
                // Fill rows, rendered ABOVE the order row (newest event on top).
                // Alpaca provides discrete fill records; SnapTrade/Webull has no
                // per-execution feed, so we derive ONE fill row from the order's own
                // filled qty + avg price — both brokers show fills, and nothing is
                // written to the DB (no P&L/engine impact).
                const fillEntries = (o.fills && o.fills.length > 0
                  ? o.fills.map((f, i) => ({ key: `${o.id}-fill-${i}`, quantity: f.quantity, price: f.price, at: f.filled_at as string | null }))
                  : (Number(o.filled_quantity) > 0 && o.filled_avg_price
                      ? [{ key: `${o.id}-fill-agg`, quantity: o.filled_quantity, price: o.filled_avg_price, at: (fillTs ?? o.closed_at) as string | null }]
                      : [])
                ).map(f => {
                  const fillNotional = Number(f.quantity) * Number(f.price);
                  const dash = <span style={{ color: "var(--faint)" }}>—</span>;
                  return (
                    <tr key={f.key} style={{ background: "var(--panel-2)" }}>
                      <td className="px-5 py-2.5 whitespace-nowrap text-xs" style={{ color: "var(--muted)" }}>
                        <span className="inline-flex items-center gap-1.5 pl-4">
                          <span style={{ color: "var(--faint)" }}>↳</span>
                          {orderSymbolLabel(o)}
                        </span>
                      </td>
                      <td className="px-5 py-2.5 num text-xs" style={{ color: "var(--text-2)" }}>{fmt(f.quantity, 0)}</td>
                      <td className="px-5 py-2.5">
                        <span className="chip uppercase font-semibold" style={{ background: o.side === "buy" ? "var(--good-soft)" : "var(--bad-soft)", color: o.side === "buy" ? "var(--good)" : "var(--bad)", borderColor: "transparent", opacity: 0.75 }}>
                          {o.side}
                        </span>
                      </td>
                      <td className="px-5 py-2.5">{dash}</td>
                      <td className="px-5 py-2.5">
                        <span className="chip uppercase tracking-wider font-medium whitespace-nowrap" style={{ background: "var(--good-soft)", color: "var(--good)", borderColor: "transparent" }}>
                          fill
                        </span>
                      </td>
                      <td className="px-5 py-2.5">{dash}</td>
                      <td className="px-5 py-2.5">{dash}</td>
                      <td className="px-5 py-2.5 num text-xs" style={{ color: "var(--text-2)" }}>{fmt(f.price, 2)}</td>
                      <td className="px-5 py-2.5">{dash}</td>
                      <td className="px-5 py-2.5">{dash}</td>
                      <td className="px-5 py-2.5 num text-xs" style={{ color: "var(--text-2)" }}>{fillNotional ? fmt(String(fillNotional)) : dash}</td>
                      <td className="px-5 py-2.5">{dash}</td>
                      <td className="px-5 py-2.5 whitespace-nowrap text-xs" style={{ color: "var(--muted)" }}>{f.at ? fmtDateTimeMs(f.at, "America/New_York") : dash}</td>
                      <td className="px-5 py-2.5">{dash}</td>
                      <td className="px-5 py-2.5">{dash}</td>
                    </tr>
                  );
                });
                return (
                  <Fragment key={o.id}>
                    {fillEntries}
                    <tr
                      className="border-t transition-colors hover:bg-[var(--panel-2)]"
                      style={{
                        borderColor: "var(--border)",
                        background: flashId === o.id ? "var(--good-soft)" : undefined,
                      }}
                    >
                      <td className="px-5 py-3.5 font-medium whitespace-nowrap" style={{ color: "var(--text)" }}>
                        {/* gap-1.5 = 6px between glyph and symbol. */}
                        <span className="inline-flex items-center gap-1.5">
                          <PositionIcon kind={orderKind(o)} />
                          {orderSymbolLabel(o)}
                        </span>
                      </td>
                      <td className="px-5 py-3.5 num">{fmt(o.quantity, 0)}</td>
                      <td className="px-5 py-3.5">
                        <span className="chip uppercase font-semibold" style={{ background: o.side === "buy" ? "var(--good-soft)" : "var(--bad-soft)", color: o.side === "buy" ? "var(--good)" : "var(--bad)", borderColor: "transparent" }}>
                          {o.side}
                        </span>
                      </td>
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
                      <td className="px-5 py-3.5">
                        <span
                          className="chip uppercase tracking-wider font-medium whitespace-nowrap"
                          style={{ background: st.bg, color: st.color, borderColor: "transparent" }}
                        >
                          {placementStatus}{o.parent_order_id ? " · copy" : ""}
                        </span>
                      </td>
                      <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: "var(--text)" }}>
                        {orderTypeLabel(o.order_type)}
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
                        // A copied mirror shows the trader's INTENDED percent
                        // verbatim, not the percent re-derived from its
                        // tick-rounded exit price.
                        const isMirror = !!o.parent_order_id;
                        const tpPct = isMirror && o.take_profit_pct != null ? Number(o.take_profit_pct) : null;
                        const slPct = isMirror && o.stop_loss_pct != null ? Number(o.stop_loss_pct) : null;
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
                                  pctOverride={tpPct}
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
                                  pctOverride={slPct}
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
        {!loading && total > 0 && (
          <div className="px-4 py-2" style={{ borderTop: "1px solid var(--border)" }}>
            <Pagination
              total={total}
              limit={limit}
              offset={offset}
              onChange={setOffset}
              pageSizeOptions={[25, 50, 100, 200]}
              onLimitChange={(n) => { setLimit(n); setOffset(0); }}
              disabled={loading}
            />
          </div>
        )}
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
