"use client";

/**
 * Today's closed orders — everything that took contracts or shares OFF a
 * position today (trims, full closes, stops that fired), shown under the
 * Positions table, each with a Re-enter: buy the same contract back, at a limit.
 * Re-enter qty starts at what the close took off and Re-enter price at its
 * fill price; both can be changed before pressing the button.
 *
 * "Closing" = the order is marked as a close, or it realized P&L (a close placed
 * in the broker's own app is only recognisable by the latter). Filled or
 * partially filled only: an exit that never traded took nothing off.
 * Selected by FILL time (filled_from), so an exit placed on an earlier day that
 * fills today is included. Refreshes on order events and every 30 s.
 * Collapsible: the header (count + realized total) stays; the choice is
 * remembered in this browser.
 *
 * Grouping: two pills beside the count — Channel (Discord users only) and
 * Symbol. Pressing one groups by it and marks it "1"; pressing the other then
 * groups within that and marks it "2". Pressing an active pill takes it out
 * (the other becomes "1"). Each group shows its count and realized P&L, and
 * starts collapsed — click its row to open it. The grouping is remembered in
 * this browser; which groups are open is not.
 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight, History } from "lucide-react";
import { api } from "@/lib/api";
import { fmtNum, fmtSignedUsd } from "@/lib/format";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { getSnapshot, USER_SNAPSHOT_KEY } from "@/lib/swrCache";
import type { Order, Position, User } from "@/lib/types";
import { positionSymbolLabel } from "@/components/OpenPositionsTable";
import { PositionSummary } from "@/components/PositionSummary";

const ET = "America/New_York";
const COLLAPSED_KEY = "positions.closedToday.collapsed";
const GROUP_KEY = "positions.closedToday.groupBy";

type GroupKey = "channel" | "symbol";
const GROUP_LABEL: Record<GroupKey, string> = { channel: "Channel", symbol: "Symbol" };

function readGroupBy(): GroupKey[] {
  try {
    const raw = JSON.parse(localStorage.getItem(GROUP_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter((k): k is GroupKey => k === "channel" || k === "symbol") : [];
  } catch {
    return [];
  }
}

/** The value a row is grouped under. Symbol is the contract as the Symbol
 *  column shows it, so a position's trims and its final close fall together. */
function groupValue(o: Order, key: GroupKey): string {
  if (key === "channel") return o.discord_channel || "No channel";
  return positionSymbolLabel(o as unknown as Position);
}

type GroupNode = { key: GroupKey; value: string; orders: Order[]; children: GroupNode[] | null };

/** Rows into nested groups, in the order of ``keys``. Groups keep the order of
 *  their newest fill, as the rows do. */
function buildGroups(rows: Order[], keys: GroupKey[]): GroupNode[] {
  if (keys.length === 0) return [];
  const [key, ...rest] = keys;
  const byValue = new Map<string, Order[]>();
  for (const o of rows) {
    const v = groupValue(o, key);
    const list = byValue.get(v);
    if (list) list.push(o);
    else byValue.set(v, [o]);
  }
  return [...byValue.entries()].map(([value, orders]) => ({
    key, value, orders, children: rest.length ? buildGroups(orders, rest) : null,
  }));
}

function sumPnl(rows: Order[]): number {
  return rows.reduce((acc, o) => acc + (Number(o.realized_pnl) || 0), 0);
}

function pnlColor(n: number | null): string {
  return n == null ? "var(--muted)" : n > 0 ? "var(--good)" : n < 0 ? "var(--bad)" : "var(--text)";
}

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function todayEt(): string {
  // en-CA formats as YYYY-MM-DD.
  return new Intl.DateTimeFormat("en-CA", { timeZone: ET }).format(new Date());
}

function fillMs(o: Order): number {
  const t = Date.parse(o.broker_filled_at || o.closed_at || "");
  return Number.isNaN(t) ? 0 : t;
}

/** Why this close can't be re-entered, or null when it can. */
function reEnterBlock(o: Order): string | null {
  if (o.side !== "sell") return "Re-enter is for closed long positions";
  if (o.instrument_type === "option" && o.option_expiry && o.option_expiry.slice(0, 10) < todayEt())
    return "This contract has expired";
  return null;
}

function defaultQty(o: Order): string {
  const n = Number(o.filled_quantity);
  if (!Number.isFinite(n) || n <= 0) return "";
  return o.instrument_type === "option" ? String(Math.round(n)) : String(n);
}

function defaultPrice(o: Order): string {
  const n = Number(o.filled_avg_price);
  return Number.isFinite(n) && n > 0 ? n.toFixed(2) : "";
}

function isClosedToday(o: Order): boolean {
  const traded = o.status === "filled" || o.status === "partially_filled";
  const closing = !!o.is_closing || (o.realized_pnl != null && o.realized_pnl !== "");
  return traded && closing;
}

export function ClosedTodayTable() {
  const [orders, setOrders] = useState<Order[] | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => setCollapsed(readCollapsed()), []);
  const toggle = () =>
    setCollapsed((c) => {
      try {
        localStorage.setItem(COLLAPSED_KEY, c ? "0" : "1");
      } catch {
        /* per-browser convenience only */
      }
      return !c;
    });
  const showChannel = !!getSnapshot<User>(USER_SNAPSHOT_KEY)?.discord_available;
  // Grouping, in the order the pills were pressed. Channel only for Discord users.
  const [groupByRaw, setGroupByRaw] = useState<GroupKey[]>([]);
  useEffect(() => setGroupByRaw(readGroupBy()), []);
  const groupBy = useMemo(
    () => groupByRaw.filter((k) => k !== "channel" || showChannel),
    [groupByRaw, showChannel],
  );
  const toggleGroup = (key: GroupKey) =>
    setGroupByRaw((cur) => {
      const next = cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key];
      try {
        localStorage.setItem(GROUP_KEY, JSON.stringify(next));
      } catch {
        /* per-browser convenience only */
      }
      return next;
    });
  // What the trader typed per row; until then the boxes show the defaults.
  const [reQty, setReQty] = useState<Record<string, string>>({});
  const [rePrice, setRePrice] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  // Rows whose position summary is open (the icon beside the symbol).
  const [summaryOpen, setSummaryOpen] = useState<Record<string, boolean>>({});
  // Groups the trader has opened. Every group starts collapsed — one line each,
  // with its count and P&L — and opens on a click. Keyed by the group's path, so
  // a regrouping starts collapsed again.
  const [groupOpen, setGroupOpen] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    try {
      const rows = await api<Order[]>(`/api/trades?filled_from=${todayEt()}&limit=500`);
      // Newest fill first (the API orders by placement time).
      setOrders(rows.filter(isClosedToday).sort((a, b) => fillMs(b) - fillMs(a)));
    } catch {
      /* keep the last list — a blip must not blank it */
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, 30_000);
    return () => clearInterval(t);
  }, [load]);

  // A fill lands as an order event; refetch shortly after, once per burst.
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEventStream((evt) => {
    if (evt.type !== "order.placed" && evt.type !== "order.updated") return;
    if (pending.current) clearTimeout(pending.current);
    pending.current = setTimeout(() => void load(), 1500);
  });

  async function reEnter(o: Order) {
    const label = positionSymbolLabel(o as unknown as Position);
    const qty = Number(reQty[o.id] ?? defaultQty(o));
    const price = Number(rePrice[o.id] ?? defaultPrice(o));
    if (!Number.isFinite(qty) || qty <= 0) return notify.warn("Enter a re-enter quantity");
    if (o.instrument_type === "option" && !Number.isInteger(qty)) return notify.warn("Options trade in whole contracts");
    if (!Number.isFinite(price) || price <= 0) return notify.warn("Enter a re-enter price");
    if (!confirm(`Re-enter ${label}: BUY ${qty} at ${price.toFixed(2)} (limit)?`)) return;
    setBusyId(o.id);
    try {
      await api<Order>(`/api/trades/${o.id}/re-enter`, {
        method: "POST",
        body: JSON.stringify({ quantity: String(qty), limit_price: price.toFixed(2) }),
      });
      notify.success(`Re-enter placed: BUY ${label} ×${qty} at ${price.toFixed(2)}`);
      void load();
    } catch (e) {
      notify.fromError(e, "re-enter failed");
    } finally {
      setBusyId(null);
    }
  }

  const total = useMemo(() => sumPnl(orders ?? []), [orders]);
  const groups = useMemo(() => buildGroups(orders ?? [], groupBy), [orders, groupBy]);

  const th = "px-4 py-2 text-left text-[11px] font-medium uppercase tracking-wide";
  const td = "px-4 py-2.5 text-[13px]";
  const inputCls = "px-2 py-1 text-xs border rounded-[var(--r-sm)] text-right num";
  const inputStyle = { borderColor: "var(--border)", background: "var(--bg)", color: "var(--text)" } as const;

  /** One closed order's row (and its position summary when open). ``depth``
   *  indents it under its groups. */
  const renderOrder = (o: Order, depth = 0) => {
    const indent = depth ? { paddingLeft: 16 + depth * 18 } : undefined;
    const pnl = o.realized_pnl == null || o.realized_pnl === "" ? null : Number(o.realized_pnl);
    const label = positionSymbolLabel(o as unknown as Position);
    const blocked = reEnterBlock(o);
    const isOption = o.instrument_type === "option";
    const open = !!summaryOpen[o.id];
    return (
      <Fragment key={o.id}>
      <tr style={{ borderTop: "1px solid var(--border)" }}>
        {showChannel && <td className={td} style={indent}>{o.discord_channel || "—"}</td>}
        <td className={`${td} whitespace-nowrap font-medium`} style={{ color: "var(--text)", ...(showChannel ? {} : indent) }}>
          <span className="inline-flex items-center gap-1.5">
            {label}
            {o.broker_account_id && <button
              type="button"
              onClick={() => setSummaryOpen((m) => ({ ...m, [o.id]: !m[o.id] }))}
              aria-expanded={open}
              aria-label={`${open ? "Hide" : "Show"} the position summary for ${label}`}
              title="Position summary — every buy and sell, with Rem.Qty"
              className="focus-ring inline-flex items-center justify-center rounded-full"
              style={{
                width: 20, height: 20, background: "var(--panel-2)", color: "var(--text-2)",
                boxShadow: open ? "0 0 0 1.5px var(--text-2)" : "none",
              }}
            >
              <History size={12} />
            </button>}
          </span>
        </td>
        <td className={`${td} num text-right`}>{fmtNum(o.filled_quantity, 0)}</td>
        <td className={`${td} capitalize`}>{String(o.order_type).replace(/_/g, " ")}</td>
        <td className={`${td} num text-right`}>{o.filled_avg_price ? fmtNum(o.filled_avg_price, 2) : "—"}</td>
        <td className={`${td} num text-right`} style={{ color: pnl == null ? "var(--muted)" : pnl > 0 ? "var(--good)" : pnl < 0 ? "var(--bad)" : "var(--text)" }}>
          {pnl == null ? "—" : fmtSignedUsd(pnl)}
        </td>
        {/* Qty, price and the button as one group, side by side. */}
        <td className={`${td} whitespace-nowrap`}>
          <div className="flex items-center gap-1.5">
            <input
              type="number" min={isOption ? 1 : 0.000001} step={isOption ? 1 : "any"}
              aria-label={`Re-enter quantity for ${label}`}
              title="Re-enter quantity"
              value={reQty[o.id] ?? defaultQty(o)}
              onChange={(e) => setReQty((s) => ({ ...s, [o.id]: e.target.value }))}
              disabled={!!blocked}
              className={`${inputCls} w-16 disabled:opacity-50`} style={inputStyle}
            />
            <span className="text-[11px]" style={{ color: "var(--muted)" }}>@</span>
            <input
              type="number" min="0.01" step="0.01"
              aria-label={`Re-enter price for ${label}`}
              title="Re-enter price (limit)"
              value={rePrice[o.id] ?? defaultPrice(o)}
              onChange={(e) => setRePrice((s) => ({ ...s, [o.id]: e.target.value }))}
              disabled={!!blocked}
              className={`${inputCls} w-20 disabled:opacity-50`} style={inputStyle}
            />
            <button
              type="button"
              onClick={() => void reEnter(o)}
              disabled={!!blocked || busyId === o.id}
              title={blocked ?? "Buy this back: a limit order for this quantity and price"}
              className="btn-primary px-3 py-1 text-[12px] disabled:opacity-50"
            >
              {busyId === o.id ? "Placing…" : "Re-enter"}
            </button>
          </div>
        </td>
      </tr>
      {open && o.broker_account_id && (
        <tr style={{ background: "var(--panel-2)" }}>
          <td colSpan={showChannel ? 7 : 6} className="px-4 py-2.5">
            <PositionSummary target={{
              brokerAccountId: o.broker_account_id,
              symbol: o.symbol,
              optionStrike: o.option_strike,
              optionRight: o.option_right,
              optionExpiry: o.option_expiry,
              throughOrderId: o.id,
            }} />
          </td>
        </tr>
      )}
      </Fragment>
    );
  };

  const cols = showChannel ? 7 : 6;
  /** A group's header row — its value, how many closes, their realized P&L —
   *  then, once opened, its subgroups or its rows. Collapsed by default. */
  const renderGroups = (nodes: GroupNode[], depth = 0, parent = ""): ReactNode[] =>
    nodes.map((g) => {
      const pnl = sumPnl(g.orders);
      const path = `${parent}/${g.key}:${g.value}`;
      const open = !!groupOpen[path];
      return (
        <Fragment key={path}>
          <tr
            style={{ borderTop: "1px solid var(--border)", background: depth === 0 ? "var(--panel-2)" : "transparent", cursor: "pointer" }}
            onClick={() => setGroupOpen((m) => ({ ...m, [path]: !m[path] }))}
          >
            <td colSpan={cols - 2} className="px-4 py-2 text-[12px]" style={{ paddingLeft: 16 + depth * 18 }}>
              <button
                type="button"
                aria-expanded={open}
                aria-label={`${open ? "Collapse" : "Expand"} ${g.value}`}
                onClick={(e) => { e.stopPropagation(); setGroupOpen((m) => ({ ...m, [path]: !m[path] })); }}
                className="focus-ring inline-flex items-center gap-1.5 text-left"
              >
                {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                <span className="font-semibold" style={{ color: "var(--text)" }}>{g.value}</span>
              </button>
              <span className="ml-2 text-[11px]" style={{ color: "var(--muted)" }}>
                {GROUP_LABEL[g.key]} · {g.orders.length} {g.orders.length === 1 ? "close" : "closes"}
              </span>
            </td>
            <td className="px-4 py-2 num text-right text-[13px] font-semibold" style={{ color: pnlColor(pnl) }}>
              {fmtSignedUsd(pnl)}
            </td>
            <td />
          </tr>
          {open && (g.children
            ? renderGroups(g.children, depth + 1, path)
            : g.orders.map((o) => renderOrder(o, depth + 1)))}
        </Fragment>
      );
    });


  return (
    <div className="card overflow-hidden">
      <div
        className="flex flex-wrap items-center justify-between gap-y-2 px-4 py-3"
        style={{ borderBottom: collapsed ? "none" : "1px solid var(--border)" }}
      >
        <button
          type="button"
          onClick={toggle}
          aria-expanded={!collapsed}
          title={collapsed ? "Show today's closed orders" : "Hide today's closed orders"}
          className="flex items-center gap-1.5 text-sm font-semibold"
          style={{ color: "var(--text)" }}
        >
          {collapsed ? <ChevronRight size={15} /> : <ChevronDown size={15} />}
          Closed today
          {orders && orders.length > 0 && (
            <span className="ml-2 text-[11px] px-2 py-0.5 rounded-full" style={{ background: "var(--panel-2)", color: "var(--muted)" }}>
              {orders.length}
            </span>
          )}
        </button>
        {!collapsed && orders && orders.length > 0 && (
          <div role="group" aria-label="Group closed orders" className="flex items-center gap-1.5 ml-2 mr-auto">
            {(showChannel ? (["channel", "symbol"] as const) : (["symbol"] as const)).map((key) => {
              const at = groupBy.indexOf(key);
              const on = at >= 0;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => toggleGroup(key)}
                  aria-pressed={on}
                  title={on
                    ? `Grouped by ${GROUP_LABEL[key].toLowerCase()}${groupBy.length > 1 ? ` (level ${at + 1})` : ""} — press to remove`
                    : `Group by ${GROUP_LABEL[key].toLowerCase()}`}
                  className="focus-ring inline-flex items-center gap-1 rounded-full text-[11px] font-medium px-2.5 py-0.5"
                  style={{
                    border: `1px solid ${on ? "var(--good)" : "var(--border)"}`,
                    background: on ? "color-mix(in srgb, var(--good) 16%, transparent)" : "var(--panel-2)",
                    color: on ? "var(--good)" : "var(--muted)",
                  }}
                >
                  {GROUP_LABEL[key]}
                  {on && (
                    <span
                      className="num inline-flex items-center justify-center rounded-full text-[10px] font-semibold"
                      style={{ width: 15, height: 15, background: "var(--good)", color: "var(--bg)" }}
                    >
                      {at + 1}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        )}
        {orders && orders.length > 0 && (
          <span className="text-[12px]" style={{ color: "var(--muted)" }}>
            Realized today{" "}
            <span className="num font-semibold" style={{ color: total > 0 ? "var(--good)" : total < 0 ? "var(--bad)" : "var(--text)" }}>
              {fmtSignedUsd(total)}
            </span>
          </span>
        )}
      </div>

      {collapsed ? null : orders === null ? (
        <div className="px-4 py-6 text-[12px]" style={{ color: "var(--muted)" }}>Loading…</div>
      ) : orders.length === 0 ? (
        <div className="px-4 py-6 text-[12px]" style={{ color: "var(--muted)" }}>Nothing closed yet today.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead style={{ color: "var(--muted)", background: "var(--panel-2)" }}>
              <tr>
                {showChannel && <th className={th}>Channel</th>}
                <th className={th}>Symbol</th>
                <th className={`${th} text-right`}>Qty</th>
                <th className={th}>Type</th>
                <th className={`${th} text-right`}>Fill price</th>
                <th className={`${th} text-right`}>Realized P&amp;L</th>
                <th className={th}>Re-enter · qty @ price</th>
              </tr>
            </thead>
            <tbody>
              {groupBy.length > 0 ? renderGroups(groups) : orders.map((o) => renderOrder(o))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
