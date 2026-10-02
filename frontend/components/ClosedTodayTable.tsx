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
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { fmtNum, fmtSignedUsd } from "@/lib/format";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { getSnapshot, USER_SNAPSHOT_KEY } from "@/lib/swrCache";
import type { Order, Position, User } from "@/lib/types";
import { positionSymbolLabel } from "@/components/OpenPositionsTable";

const ET = "America/New_York";
const COLLAPSED_KEY = "positions.closedToday.collapsed";

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
  // What the trader typed per row; until then the boxes show the defaults.
  const [reQty, setReQty] = useState<Record<string, string>>({});
  const [rePrice, setRePrice] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState<string | null>(null);

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

  const total = useMemo(
    () => (orders ?? []).reduce((acc, o) => acc + (Number(o.realized_pnl) || 0), 0),
    [orders],
  );

  const th = "px-4 py-2 text-left text-[11px] font-medium uppercase tracking-wide";
  const td = "px-4 py-2.5 text-[13px]";
  const inputCls = "w-20 px-2 py-1 text-xs border rounded-[var(--r-sm)] text-right num";
  const inputStyle = { borderColor: "var(--border)", background: "var(--bg)", color: "var(--text)" } as const;

  return (
    <div className="card overflow-hidden">
      <div
        className="flex items-center justify-between px-4 py-3"
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
                <th className={`${th} text-right`}>Re-enter qty</th>
                <th className={`${th} text-right`}>Re-enter price</th>
                <th className={th}><span className="sr-only">Re-enter</span></th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => {
                const pnl = o.realized_pnl == null || o.realized_pnl === "" ? null : Number(o.realized_pnl);
                const label = positionSymbolLabel(o as unknown as Position);
                const blocked = reEnterBlock(o);
                const isOption = o.instrument_type === "option";
                return (
                  <tr key={o.id} style={{ borderTop: "1px solid var(--border)" }}>
                    {showChannel && <td className={td}>{o.discord_channel || "—"}</td>}
                    <td className={`${td} whitespace-nowrap font-medium`} style={{ color: "var(--text)" }}>{label}</td>
                    <td className={`${td} num text-right`}>{fmtNum(o.filled_quantity, 0)}</td>
                    <td className={`${td} capitalize`}>{String(o.order_type).replace(/_/g, " ")}</td>
                    <td className={`${td} num text-right`}>{o.filled_avg_price ? fmtNum(o.filled_avg_price, 2) : "—"}</td>
                    <td className={`${td} num text-right`} style={{ color: pnl == null ? "var(--muted)" : pnl > 0 ? "var(--good)" : pnl < 0 ? "var(--bad)" : "var(--text)" }}>
                      {pnl == null ? "—" : fmtSignedUsd(pnl)}
                    </td>
                    <td className={`${td} text-right`}>
                      <input
                        type="number" min={isOption ? 1 : 0.000001} step={isOption ? 1 : "any"}
                        aria-label={`Re-enter quantity for ${label}`}
                        value={reQty[o.id] ?? defaultQty(o)}
                        onChange={(e) => setReQty((s) => ({ ...s, [o.id]: e.target.value }))}
                        disabled={!!blocked}
                        className={`${inputCls} disabled:opacity-50`} style={inputStyle}
                      />
                    </td>
                    <td className={`${td} text-right`}>
                      <input
                        type="number" min="0.01" step="0.01"
                        aria-label={`Re-enter price for ${label}`}
                        value={rePrice[o.id] ?? defaultPrice(o)}
                        onChange={(e) => setRePrice((s) => ({ ...s, [o.id]: e.target.value }))}
                        disabled={!!blocked}
                        className={`${inputCls} disabled:opacity-50`} style={inputStyle}
                      />
                    </td>
                    <td className={`${td} text-right whitespace-nowrap`}>
                      <button
                        type="button"
                        onClick={() => void reEnter(o)}
                        disabled={!!blocked || busyId === o.id}
                        title={blocked ?? "Buy this back: a limit order for the quantity and price on the left"}
                        className="btn-primary px-3 py-1 text-[12px] disabled:opacity-50"
                      >
                        {busyId === o.id ? "Placing…" : "Re-enter"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
