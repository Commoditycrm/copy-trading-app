"use client";

import { Fragment, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, forwardRef } from "react";
import { motion } from "framer-motion";
import { AlertTriangle, ArrowDown, ArrowUp, ChevronDown, ChevronUp, ChevronsUpDown, Layers, Search, TrendingDown, TrendingUp, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { getSnapshot, setSnapshot, USER_SNAPSHOT_KEY } from "@/lib/swrCache";
import { fmtDate, fmtDateTimeMs, fmtDuration, fmtUsd, fmtSignedUsd } from "@/lib/format";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { useLivePrice, useLivePrices, peekLivePrice } from "@/lib/livePrices";
import { useTableColumns, type ColumnDef, type ResolvedColumn } from "@/lib/useTableColumns";
import { ColumnsMenu, ResizeHandle } from "@/components/ColumnsMenu";
import { Spinner } from "@/components/Spinner";
import { PositionIcon, positionKind } from "@/components/PositionIcon";
import { AnimatedNumber } from "@/components/dashboard/AnimatedNumber";
import { InlineBracketCell } from "@/components/InlineBracketCell";
import type { BrokerAccount, Order, Position, PositionsPayload, UnreachableAccount, User } from "@/lib/types";

type PosSnap = { positions: Position[]; orders: Order[] };
const POS_KEY = "positions:table";

function fmtNum(n: string | null | undefined, dp = 2): string {
  if (n === null || n === undefined || n === "") return "—";
  const v = Number(n);
  if (!Number.isFinite(v)) return String(n);
  return v.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

/** Current-price cell that ticks live off the central stream. `symbol` is the
 *  ticker for stocks / the OCC for options (both streamed now), or null when
 *  unbuildable — then it falls through to the fallback. Own component so the
 *  hook stays out of the row's .map(). */
function LiveCurrentPriceCell({ symbol, fallback }: { symbol: string | null; fallback: string | null }) {
  const live = useLivePrice(symbol, fallback);
  return <td className="px-5 py-3.5 num">{fmtNum(live == null ? fallback : String(live), 2)}</td>;
}

/** Change in a stock row's market value and unrealized P&L implied by the live
 *  price: signed_qty × (livePrice − snapshotPrice). Both move by the same
 *  amount, so we layer this on the backend's already-correct baseline rather
 *  than re-deriving sign/multiplier conventions in the UI. Returns 0 until a
 *  tick arrives, or for options (symbol null) — so the row shows exactly the
 *  backend values until the price actually moves. */
function useLivePriceDelta(
  symbol: string | null, snapshotPrice: string | null, quantity: string | null, multiplier = 1,
): number {
  const live = useLivePrice(symbol, null);
  if (live == null) return 0;
  const snap = Number(snapshotPrice);
  const qty = Number(quantity);
  if (!Number.isFinite(snap) || !Number.isFinite(qty)) return 0;
  // Options move $100 of value per $1 of quote (contract multiplier); stocks 1.
  return (live - snap) * qty * multiplier;
}

/** Unrealized P&L cell, moved live off the price delta. */
function LivePnlCell({ symbol, snapshotPrice, quantity, baseline, multiplier = 1 }: {
  symbol: string | null; snapshotPrice: string | null; quantity: string | null; baseline: string | null; multiplier?: number;
}) {
  const delta = useLivePriceDelta(symbol, snapshotPrice, quantity, multiplier);
  const val = baseline == null || baseline === "" ? null : Number(baseline) + delta;
  const pnl = fmtSignedMoney(val == null ? null : String(val));
  return (
    <td className="px-5 py-3.5 num font-medium">
      <span className="inline-flex items-center gap-1" style={{ color: pnl.sign === 1 ? "var(--good)" : pnl.sign === -1 ? "var(--bad)" : "var(--muted)" }}>
        {pnl.sign === 1 && <TrendingUp size={13} />}
        {pnl.sign === -1 && <TrendingDown size={13} />}
        {pnl.text}
      </span>
    </td>
  );
}

/** P&L % = live unrealized P&L / cost basis. Mirrors the static formula. */
function LivePnlPctCell({ symbol, snapshotPrice, quantity, unrealizedBaseline, costBasis, multiplier = 1 }: {
  symbol: string | null; snapshotPrice: string | null; quantity: string | null;
  unrealizedBaseline: string | null; costBasis: string | null; multiplier?: number;
}) {
  const delta = useLivePriceDelta(symbol, snapshotPrice, quantity, multiplier);
  const cb = Number(costBasis);
  const upnl = Number(unrealizedBaseline) + delta;
  const pct = Number.isFinite(cb) && cb !== 0 && Number.isFinite(upnl) ? (upnl / Math.abs(cb)) * 100 : null;
  return (
    <td className="px-5 py-3.5 num" style={{ color: pct == null ? "var(--muted)" : pct > 0 ? "var(--good)" : pct < 0 ? "var(--bad)" : "var(--text-2)" }}>
      {pct == null ? "—" : `${pct > 0 ? "+" : ""}${pct.toFixed(2)}%`}
    </td>
  );
}

/** Market-value cell, moved live off the price delta. */
function LiveMarketValueCell({ symbol, snapshotPrice, quantity, baseline, multiplier = 1 }: {
  symbol: string | null; snapshotPrice: string | null; quantity: string | null; baseline: string | null; multiplier?: number;
}) {
  const delta = useLivePriceDelta(symbol, snapshotPrice, quantity, multiplier);
  const val = baseline == null || baseline === "" ? null : Number(baseline) + delta;
  return <td className="px-5 py-3.5 num">{fmtNum(val == null ? baseline : String(val), 2)}</td>;
}

/** Net liquidity cell — the position's live liquidation value, computed directly
 *  as price × signed_qty × multiplier rather than off the backend market_value.
 *  That keeps it correctly signed (short = negative) no matter how each broker
 *  reports market_value (some send it unsigned). Falls back to the snapshot
 *  price until a tick arrives. */
function LiveNetLiqCell({ symbol, snapshotPrice, quantity, multiplier = 1 }: {
  symbol: string | null; snapshotPrice: string | null; quantity: string | null; multiplier?: number;
}) {
  const live = useLivePrice(symbol, null);
  const price = live != null ? live : Number(snapshotPrice);
  const qty = Number(quantity);
  const val = Number.isFinite(price) && Number.isFinite(qty) ? price * qty * multiplier : null;
  return <td className="px-5 py-3.5 num">{val == null ? "—" : fmtNum(String(val), 2)}</td>;
}

type ExitMode = "close" | "average";

/** The ▾ half of a split button: switches what its button does for this row
 *  between closing the position and averaging into it (buying more). Optional
 *  one-off ``actions`` render below the modes, after a divider. */
function ModeCaret({ mode, labels, onChange, disabled, variant, actions = [] }: {
  mode: ExitMode; labels: Record<ExitMode, string>; onChange: (m: ExitMode) => void;
  disabled?: boolean; variant: "ghost" | "solid";
  actions?: { label: string; onClick: () => void; danger?: boolean }[];
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  return (
    <div ref={ref} className="relative flex">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen(o => !o)}
        aria-label="Choose close or average"
        aria-haspopup="menu"
        aria-expanded={open}
        className={`${variant === "solid" ? "btn-accent-solid" : "btn-ghost"} px-0.5 py-1 text-xs inline-flex items-center`}
        style={{
          borderTopLeftRadius: 0, borderBottomLeftRadius: 0,
          borderTopRightRadius: "var(--r-sm)", borderBottomRightRadius: "var(--r-sm)",
          borderLeft: "1px solid rgba(255,255,255,0.15)",
          background: mode === "average" ? "var(--good)" : undefined,
          color: mode === "average" ? "#fff" : undefined,
        }}
      >
        <ChevronDown size={12} />
      </button>
      {open && (
        <div role="menu" className="absolute right-0 top-full z-20 mt-1 flex flex-col rounded-lg py-1 text-xs shadow-lg"
             style={{ background: "var(--panel)", border: "1px solid var(--border)", minWidth: 150 }}>
          {(["close", "average"] as const).map(m => (
            <button
              key={m}
              type="button"
              role="menuitemradio"
              aria-checked={mode === m}
              onClick={() => { onChange(m); setOpen(false); }}
              className="block w-full text-left px-3 py-1.5 hover:bg-[var(--panel-2)] whitespace-nowrap"
              style={{ color: mode === m ? "var(--accent)" : "var(--text-2)" }}
            >
              {labels[m]}
            </button>
          ))}
          {actions.length > 0 && <div style={{ borderTop: "1px solid var(--border)", margin: "2px 0" }} />}
          {actions.map(a => (
            <button
              key={a.label}
              type="button"
              role="menuitem"
              onClick={() => { setOpen(false); a.onClick(); }}
              className="block w-full text-left px-3 py-1.5 hover:bg-[var(--panel-2)] whitespace-nowrap"
              style={{ color: a.danger ? "var(--bad)" : "var(--text-2)" }}
            >
              {a.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Stop levels offered on the expanded row, as the position's P&L: -25 puts the
 *  stop 25% below entry, 0 at break-even, +25 locks in a quarter. */
const STOP_LEVELS = [-25, -10, 0, 25];

/** Width of the first control in both action rows (Close at Market above,
 *  Stop + X.Stops below), so the inputs after it line up in one column. */
const ACTION_SLOT_W = 116;

/** The row that opens under a position (down arrow in Actions): the same two
 *  columns as the main row, repurposed for protection. Close % becomes the stop
 *  level; Actions holds Stop (set it), X.Stops (cancel them), and the trailing % with
 *  T.Stop. Every other column renders empty so the two cells sit exactly
 *  under their counterparts in the user's own column order. */
function PositionStopRow({ columnIds, orderId, hasStop, ladderStop, entryPrice, label, brokerSymbol, brokerAccountId, onDone }: {
  columnIds: string[]; orderId: string | null; hasStop: boolean; ladderStop: string | null;
  entryPrice: number | null; label: string;
  brokerSymbol: string; brokerAccountId: string; onDone: () => void;
}) {
  // The entry's bracket SL is cleared through the bracket endpoint; everything
  // else (ladder stop, trailing exit, resting stop orders) through stops/cancel.
  const bracketStop = !!orderId && hasStop;
  const [level, setLevel] = useState<number | null>(null);
  const [busy, setBusy] = useState<null | "set" | "stop" | "trail">(null);
  const [trailPct, setTrailPct] = useState("");
  const account = `broker_account_id=${brokerAccountId}`;
  const base = `/api/positions/${encodeURIComponent(brokerSymbol)}`;

  const levelPrice = (pct: number) =>
    entryPrice != null ? Math.floor(entryPrice * (1 + pct / 100) * 100) / 100 : null;

  async function setStop() {
    if (level == null) return;
    setBusy("set");
    try {
      const res = await api<{ stop_price: string }>(`${base}/stop?${account}&pnl_pct=${level}`, { method: "POST" });
      notify.success(`Stop set at ${level > 0 ? "+" : ""}${level}% P&L (${res.stop_price}) — placed at the broker within a few seconds`);
      setLevel(null);
      onDone();
    } catch (e) { notify.fromError(e, "Could not set the stop"); }
    finally { setBusy(null); }
  }

  async function cancelStops() {
    const which = ladderStop != null ? ` (stop @ ${ladderStop})` : "";
    if (!confirm(`Cancel every stop on ${label}${which}, including a trailing stop? The position stays open, and the ladder won't put them back.`)) return;
    setBusy("stop");
    let removed = 0;
    try {
      try {
        const res = await api<{ removed: string[] }>(`${base}/stops/cancel?${account}`, { method: "POST" });
        removed += res.removed.length;
      } catch (e) {
        // 404 "no_stops" just means nothing of that kind was set.
        if (!(e instanceof ApiError && e.status === 404)) throw e;
      }
      if (bracketStop) {
        // Clearing the SL leg cancels the live stop order (same path as emptying
        // the inline SL field). Key presence flags sl_present on the backend.
        await api(`/api/trades/${orderId}/bracket`, { method: "PATCH", body: JSON.stringify({ stop_loss_price: null }) });
        removed += 1;
      }
      if (removed) {
        notify.success("Stops cancelled");
        onDone();
      } else {
        notify.info("No stops on this position");
      }
    } catch (e) { notify.fromError(e, "Could not cancel stops"); }
    finally { setBusy(null); }
  }

  async function armTrail() {
    const pct = parseFloat(trailPct);
    if (!Number.isFinite(pct) || pct <= 0 || pct > 100) { notify.warn("Enter a trail % between 0 and 100"); return; }
    setBusy("trail");
    try {
      const res = await api<{ mode?: string }>(`${base}/trailing-stop?${account}&trail_percent=${pct}`, { method: "POST" });
      notify.success(`Trailing stop armed ${pct}% below the market${res.mode === "emulated" ? " (app-monitored)" : ""}`);
      setTrailPct("");
      onDone();
    } catch (err) { notify.fromError(err, "Could not arm trailing stop"); }
    finally { setBusy(null); }
  }

  const cells: Record<string, React.ReactNode> = {
    close_pct: (
      <td className="px-5 pb-3 pt-1">
        <div className="flex gap-1">
          {STOP_LEVELS.map(pct => {
            const selected = level === pct;
            const px = levelPrice(pct);
            return (
              <button
                key={pct}
                type="button"
                onClick={() => setLevel(selected ? null : pct)}
                title={px != null ? `Stop at ${px.toFixed(2)} (${pct > 0 ? "+" : ""}${pct}% P&L from entry)` : `${pct}% P&L from entry`}
                className="px-2 py-0.5 text-[10px] rounded transition-colors"
                style={{
                  border: `1px solid ${selected ? "rgba(10,115,168,0.4)" : "var(--border)"}`,
                  background: selected ? "var(--nav-active-bg)" : "transparent",
                  color: selected ? "var(--accent)" : "var(--text-2)",
                }}
              >
                {pct}%
              </button>
            );
          })}
        </div>
      </td>
    ),
    actions: (
      <td className="px-5 pb-3 pt-1">
        <div className="flex gap-2 items-center whitespace-nowrap">
          {/* Same width as the main row's Close at Market, so the input below
              lines up under the Limit field. */}
          <div className="flex gap-1 justify-between" style={{ width: ACTION_SLOT_W }}>
            <button
              type="button"
              disabled={level == null || busy !== null}
              onClick={setStop}
              title={level == null ? "Pick a stop level first" : `Set the stop at ${level > 0 ? "+" : ""}${level}% P&L`}
              className="btn-ghost px-2 py-1 text-xs inline-flex items-center justify-center gap-1 disabled:opacity-40"
            >
              <span>Stop</span>
              {busy === "set" && <Spinner />}
            </button>
            <button
              type="button"
              disabled={busy !== null}
              onClick={cancelStops}
              title="Cancel every stop on this position — the stop, a trailing stop, and a bracket stop-loss"
              className="btn-ghost px-2 py-1 text-xs inline-flex items-center justify-center gap-1 disabled:opacity-40"
            >
              <span>X.Stops</span>
              {busy === "stop" && <Spinner />}
            </button>
          </div>
          <div className="flex items-stretch">
            <input
              type="number" step="0.1" min="0.1" max="100"
              placeholder="-% mkt"
              aria-label={`Trailing stop percent below market for ${label}`}
              value={trailPct}
              onChange={e => setTrailPct(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter" && trailPct) void armTrail(); }}
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
              type="button"
              disabled={busy !== null || !trailPct}
              onClick={armTrail}
              className="btn-danger px-3 py-1 text-xs font-medium inline-flex items-center gap-1.5 disabled:opacity-40"
              style={{
                borderTopLeftRadius: 0,
                borderBottomLeftRadius: 0,
                borderTopRightRadius: "var(--r-sm)",
                borderBottomRightRadius: "var(--r-sm)",
              }}
            >
              <span>T.Stop</span>
              {busy === "trail" && <Spinner />}
            </button>
          </div>
        </div>
      </td>
    ),
  };
  return (
    <tr style={{ background: "var(--panel-2)" }}>
      {columnIds.map(id => <Fragment key={id}>{cells[id] ?? <td />}</Fragment>)}
    </tr>
  );
}

/** The two summary tiles that move with price — Unrealized P&L and Market value.
 *  Own component so a tick only re-renders these tiles, not the whole table.
 *  Sums each visible stock row's live delta (same signed_qty × Δprice as the
 *  per-row cells) on top of the backend baseline; 0 until ticks arrive. */
function LiveTotalsTiles({ positions, baselinePnl, baselineMv, mvSub }: {
  positions: Position[]; baselinePnl: number; baselineMv: number; mvSub: string;
}) {
  // Each streamable row → its cache key (ticker or OCC), contract multiplier,
  // snapshot price and signed qty. Stocks and options both stream now.
  const rows = useMemo(
    () => positions.map((p) => ({
      sym: p.instrument_type === "stock" ? p.symbol.toUpperCase() : positionOcc(p),
      mult: p.instrument_type === "option" ? 100 : 1,
      snap: Number(p.current_price),
      qty: Number(p.quantity),
    })).filter((r): r is { sym: string; mult: number; snap: number; qty: number } => !!r.sym),
    [positions],
  );
  const symbols = useMemo(() => rows.map((r) => r.sym), [rows]);
  const version = useLivePrices(symbols);
  const { pnl, mv } = useMemo(() => {
    let d = 0;
    for (const r of rows) {
      const live = peekLivePrice(r.sym);
      if (live == null || !Number.isFinite(r.snap) || !Number.isFinite(r.qty)) continue;
      d += (live - r.snap) * r.qty * r.mult;
    }
    return { pnl: baselinePnl + d, mv: baselineMv + d };
    // version drives the recompute when a subscribed symbol ticks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, baselinePnl, baselineMv, version]);
  return (
    <>
      <SummaryTile label="Unrealized P&L" tone={pnl > 0 ? "good" : pnl < 0 ? "bad" : "neutral"}
        node={<AnimatedNumber value={pnl} format={fmtSignedUsd} className="num" />}
        sub="On open positions" />
      <SummaryTile label="Market value" tone="neutral"
        node={<AnimatedNumber value={mv} format={fmtUsd} className="num" />}
        sub={mvSub} />
    </>
  );
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

/** Option expiry as "10 Jul 26" (Webull style). Input is an ISO "YYYY-MM-DD". */
function optionExpiryShort(isoDate: string): string {
  const d = new Date(isoDate.length === 10 ? isoDate + "T00:00:00Z" : isoDate);
  if (Number.isNaN(d.getTime())) return isoDate;
  const mon = d.toLocaleDateString("en-US", { month: "short", timeZone: "UTC" });
  return `${d.getUTCDate()} ${mon} ${String(d.getUTCFullYear()).slice(-2)}`;
}

/** Full descriptor shown in the Symbol column, Webull style:
 *  stock  → "META";  option → "META C $372 10 Jul 26". */
function positionSymbolLabel(p: Position): string {
  if (p.instrument_type !== "option") return p.symbol.toUpperCase();
  const cp = p.option_right === "call" ? "C" : p.option_right === "put" ? "P" : "";
  const strike = p.option_strike != null && p.option_strike !== ""
    ? `$${Number(p.option_strike)}` : "";
  const exp = p.option_expiry ? optionExpiryShort(p.option_expiry) : "";
  return [p.symbol.toUpperCase(), cp, strike, exp].filter(Boolean).join(" ");
}

/** OCC symbol for an option position — must match the backend's _build_occ
 *  (ROOT + YYMMDD + C/P + strike*1000, 8 digits, root not padded) so it looks up
 *  the same cache key the option stream writes. Null for stocks / incomplete. */
function positionOcc(p: Position): string | null {
  if (p.instrument_type !== "option") return null;
  if (!p.option_expiry || p.option_strike == null || p.option_strike === "" || !p.option_right) return null;
  const iso = p.option_expiry;
  const d = new Date(iso.length === 10 ? iso + "T00:00:00Z" : iso);
  if (Number.isNaN(d.getTime())) return null;
  const yy = String(d.getUTCFullYear()).slice(-2);
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const cp = p.option_right === "call" ? "C" : "P";
  const strikeInt = Math.round(Number(p.option_strike) * 1000);
  if (!Number.isFinite(strikeInt)) return null;
  return `${p.symbol.toUpperCase()}${yy}${mm}${dd}${cp}${String(strikeInt).padStart(8, "0")}`;
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
  | "channel" | "symbol" | "quantity" | "avg_entry_price" | "current_price"
  | "market_value" | "net_liq" | "unrealized_pnl" | "day_pnl" | "expires";

function sortValue(p: Position, key: SortKey): number | string {
  switch (key) {
    // Blank channels sort last (a high code point) rather than jumping to the top.
    case "channel": return (p.discord_channel || "￿").toUpperCase();
    case "symbol": return p.symbol.toUpperCase();
    case "quantity": return Math.abs(Number(p.quantity)) || 0;
    case "avg_entry_price": return Number(p.avg_entry_price) || 0;
    case "current_price": return Number(p.current_price) || 0;
    case "market_value": return Number(p.market_value) || 0;
    case "net_liq": return (Number(p.current_price) || 0) * (Number(p.quantity) || 0) * (p.instrument_type === "option" ? 100 : 1);
    case "unrealized_pnl": return Number(p.unrealized_pnl) || 0;
    case "day_pnl": return p.day_pnl != null ? Number(p.day_pnl) : Number.NEGATIVE_INFINITY;
    case "expires": return p.option_expiry ? daysUntil(p.option_expiry) : Number.POSITIVE_INFINITY;
  }
}

export interface OpenPositionsTableHandle {
  /** Force a refresh from /api/positions. Call after placing/exiting orders. */
  refresh: () => Promise<void>;
}

/** Order statuses that mean "still live at the broker" — something can still
 *  fill, so the positions view can still change. */
const WORKING_STATUSES = new Set(["pending", "submitted", "accepted", "partially_filled"]);

export const OpenPositionsTable = forwardRef<OpenPositionsTableHandle, { className?: string; fillHeight?: boolean }>(
  function OpenPositionsTable({ className, fillHeight }, ref) {
    // Stale-while-revalidate: paint the last positions/orders instantly on
    // return nav, then refresh() below revalidates. Cleared on logout.
    // The Channel column is only meaningful to a trader who has Discord — for
    // everyone else every row reads "—", a column of nothing. Both pages that
    // host this table already load the user into the shared snapshot, so this
    // is normally free; the fetch is only for a cold start (a hard refresh
    // straight onto the trade panel).
    const [showChannel, setShowChannel] = useState<boolean>(
      () => !!getSnapshot<User>(USER_SNAPSHOT_KEY)?.discord_enabled,
    );

    // Configurable columns (per-user, synced). The functional columns —
    // Symbol, Close %, Actions, TP, SL — are locked (can't be hidden) so their
    // per-row controls always render; every data column can be toggled off.
    const columnDefs = useMemo<ColumnDef[]>(() => [
      ...(showChannel ? [{ id: "channel", header: "Channel" }] : []),
      { id: "symbol", header: "Symbol", locked: true },
      { id: "qty", header: "Qty" },
      { id: "side", header: "Side" },
      { id: "close_pct", header: "Close %", locked: true },
      { id: "actions", header: "Actions", locked: true },
      { id: "unrealized_pnl", header: "Unrealized P&L" },
      { id: "pnl_pct", header: "P&L %" },
      { id: "day_pnl", header: "Day's P&L" },
      { id: "avg_entry", header: "Avg entry" },
      { id: "current_price", header: "Current price" },
      { id: "pdc", header: "PDC" },
      { id: "filled_price", header: "Filled price" },
      { id: "market_value", header: "Market value" },
      { id: "net_liq", header: "Net Liq" },
      { id: "tp", header: "TP", locked: true },
      { id: "sl", header: "SL", locked: true },
      { id: "submitted_at", header: "Submitted at" },
      { id: "filled_at", header: "Filled at" },
      { id: "time_taken", header: "Time Taken to Filled" },
      { id: "expires", header: "Expires in Days" },
    ], [showChannel]);
    const cols = useTableColumns("positions", columnDefs);
    useEffect(() => {
      if (getSnapshot<User>(USER_SNAPSHOT_KEY)) return;
      let cancelled = false;
      api<User>("/api/auth/me")
        .then((u) => {
          if (cancelled) return;
          setSnapshot(USER_SNAPSHOT_KEY, u);
          setShowChannel(!!u.discord_enabled);
        })
        .catch(() => {});   // the table still works without it; the column hides
      return () => { cancelled = true; };
    }, []);

    const [positions, setPositions] = useState<Position[]>(() => getSnapshot<PosSnap>(POS_KEY)?.positions ?? []);
    const [orders, setOrders] = useState<Order[]>(() => getSnapshot<PosSnap>(POS_KEY)?.orders ?? []);
    // Today's realized P&L (market tz), fetched alongside positions for the
    // summary strip. null until the first fetch lands.
    // Broker accounts whose positions could not be read on the last refresh.
    // Rendered as a banner: an empty table with no explanation reads as "you
    // hold nothing", which is exactly the wrong thing to tell someone whose
    // broker just failed to answer.
    const [unreachable, setUnreachable] = useState<UnreachableAccount[]>([]);
    const [todayRealized, setTodayRealized] = useState<number | null>(null);
    // Total account value = sum of total_equity across broker accounts (same
    // source as the dashboard "Total equity" KPI). null until first fetch.
    const [totalEquity, setTotalEquity] = useState<number | null>(null);
    const [loading, setLoading] = useState(() => getSnapshot<PosSnap>(POS_KEY) === undefined);
    const [closing, setClosing] = useState<{ key: string; kind: "market" | "limit" } | null>(null);
    const [closeLimitPrices, setCloseLimitPrices] = useState<Record<string, string>>({});
    // Positions whose stop row (down arrow in Actions) is open.
    const [expanded, setExpanded] = useState<Record<string, boolean>>({});
    // Per row, what each split button does: close the position, or average
    // into it (buy more). Chosen from the ▾ beside the button.
    const [marketMode, setMarketMode] = useState<Record<string, ExitMode>>({});
    const [limitMode, setLimitMode] = useState<Record<string, ExitMode>>({});
    // Per-row close size as a percentage of the held quantity. Defaults to 100%.
    const [closePercents, setClosePercents] = useState<Record<string, number>>({});
    // Filter: default to options since that's the most common workflow here.
    const [filter, setFilter] = useState<"all" | "stock" | "option">("all");
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

    // Monotonic id for in-flight refreshes. Without it, two overlapping
    // refreshes both write state and the one that RESOLVES last wins — which
    // can be the older request, silently replacing fresh data with stale.
    const reqSeq = useRef(0);
    // True while any order is still live at the broker. Drives the early exit
    // from the staggered refresh schedule below.
    const workingRef = useRef(false);

    const refresh = useCallback(async () => {
      const seq = ++reqSeq.current;
      try {
        const [payload, ords, realized, brokers] = await Promise.all([
          // ?detail=1 returns { positions, unreachable } so a broker that
          // failed to answer is distinguishable from one holding nothing.
          api<PositionsPayload>("/api/positions?detail=1"),
          api<Order[]>("/api/trades").catch(() => [] as Order[]),
          api<{ realized_pnl: number }>("/api/positions/today-realized").catch(() => null),
          api<BrokerAccount[]>("/api/brokers").catch(() => [] as BrokerAccount[]),
        ]);
        // A newer refresh already landed — drop this one rather than undo it.
        if (seq !== reqSeq.current) return;
        const pos = payload.positions ?? [];
        const down = payload.unreachable ?? [];
        setPositions(pos);
        setOrders(ords);
        setUnreachable(down);
        workingRef.current = ords.some((o) => WORKING_STATUSES.has(o.status));
        if (realized) setTodayRealized(realized.realized_pnl);
        setTotalEquity(brokers.reduce((acc, b) => acc + (Number(b.total_equity) || 0), 0));
        // Never persist a KNOWN-INCOMPLETE view. The snapshot seeds initial
        // state on return nav, so caching a truncated read makes one failed
        // broker call look like a flat account long after it recovered.
        if (down.length === 0) {
          setSnapshot<PosSnap>(POS_KEY, { positions: pos, orders: ords });
        }
      } catch (e) {
        if (seq !== reqSeq.current) return;
        notify.fromError(e, "failed to load positions");
      } finally {
        if (seq === reqSeq.current) setLoading(false);
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
        evt.type !== "order.updated" &&
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
        ssTimers.current.push(setTimeout(async () => {
          await refresh();
          // The stagger exists to catch a fill we get no SSE for. Once nothing
          // is working any more there is nothing left to catch, so cancel the
          // rest instead of firing them blind. Each one costs a broker call,
          // and on Webull four of them arriving together is what triggers the
          // 429 that blanks this table in the first place.
          if (!workingRef.current) clearTimers();
        }, ms));
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

    /** Cancel the open orders on THIS row's contract only — not the account.
     *  Subscribers' mirrors of those orders are cancelled too. */
    async function cancelPositionOpenOrders(p: Position) {
      const label = p.symbol.toUpperCase();
      if (!confirm(
        `Cancel the open orders on ${label} (${p.broker_symbol})? Only this position's orders are cancelled; ` +
        "for a trader, subscribers' mirrors of them go too. A Discord ladder stop on it is removed and stays removed."
      )) return;
      try {
        const res = await api<{ cancelled_count?: number }>(
          `/api/positions/${encodeURIComponent(p.broker_symbol)}/cancel-open?broker_account_id=${p.broker_account_id}&include_subscribers=true`,
          { method: "POST" },
        );
        const n = res.cancelled_count ?? 0;
        if (n) notify.success(`Cancelled ${n} open order${n === 1 ? "" : "s"} on ${label}`);
        else notify.info(`No open orders on ${label}`);
        refresh();
      } catch (e) {
        notify.fromError(e, "Could not cancel open orders");
      }
    }

    async function averagePosition(p: Position, type: "market" | "limit") {
      const key = posKey(p);
      if (type === "limit") {
        const price = closeLimitPrices[key];
        if (!price || Number(price) <= 0) {
          notify.warn("Enter a limit price");
          return;
        }
      }
      // Sized off what is held now, like a close: 50% of 10 adds 5.
      const pct = closePercents[key] ?? 100;
      const qty = quantityForPercent(p, pct);
      if (qty == null) {
        notify.warn(`Can't average ${pct}% of this position — would round to zero.`);
        return;
      }
      const at = type === "limit" ? `at ${closeLimitPrices[key]}` : "at market";
      if (!confirm(`Average into ${p.symbol.toUpperCase()}: BUY ${qty} more ${at} (${pct}% of what you hold)?`)) return;
      setClosing({ key, kind: type });
      try {
        const body: Record<string, unknown> = { order_type: type, quantity: String(qty) };
        if (type === "limit") body.limit_price = closeLimitPrices[key];
        const order = await api<Order>(
          `/api/positions/${encodeURIComponent(p.broker_symbol)}/average?broker_account_id=${p.broker_account_id}`,
          { method: "POST", body: JSON.stringify(body) },
        );
        notify.success(`Average placed: BUY ${order.symbol} ×${qty} (${type})`);
        if (type === "limit") setCloseLimitPrices(s => ({ ...s, [key]: "" }));
        refresh();
      } catch (e) {
        notify.fromError(e, "average failed");
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

    const Th = ({ label, sortKey, className: thc = "", title, col }: { label: string; sortKey?: SortKey; className?: string; title?: string; col?: ResolvedColumn }) => {
      const active = sortKey && sort?.key === sortKey;
      const w = col?.width;
      return (
        <th
          title={title}
          className={`relative text-left px-5 py-3 font-medium whitespace-nowrap select-none ${thc}`}
          style={{ color: active ? "var(--text-2)" : "var(--muted)", ...(w ? { width: w, minWidth: w, maxWidth: w } : {}) }}
        >
          {sortKey ? (
            <button type="button" onClick={() => toggleSort(sortKey)} className="inline-flex items-center gap-1 focus-ring rounded hover:text-[var(--text)] transition-colors uppercase tracking-[0.06em] text-[11px]" style={{ color: "inherit" }}>
              {label}
              <SortIcon k={sortKey} />
            </button>
          ) : label}
          {col && <ResizeHandle minWidth={col.minWidth ?? 60} onResize={(px) => cols.setWidth(col.id, px)} />}
        </th>
      );
    };

    // Header metadata by column id — label + optional sort key/title. Drives the
    // ordered header render below.
    const HEADER_META: Record<string, { label: string; sortKey?: SortKey; title?: string }> = {
      channel: { label: "Channel", sortKey: "channel", title: "Discord channel whose alert opened this position" },
      symbol: { label: "Symbol", sortKey: "symbol" },
      qty: { label: "Qty", sortKey: "quantity" },
      side: { label: "Side" },
      close_pct: { label: "Close %" },
      actions: { label: "Actions" },
      unrealized_pnl: { label: "Unrealized P&L", sortKey: "unrealized_pnl" },
      pnl_pct: { label: "P&L %", title: "Unrealized P&L as a % of cost basis" },
      day_pnl: { label: "Day's P&L", sortKey: "day_pnl", title: "Today's P&L on this position, straight from your broker (Webull / Alpaca)" },
      avg_entry: { label: "Avg entry", sortKey: "avg_entry_price" },
      current_price: { label: "Current price", sortKey: "current_price" },
      pdc: { label: "PDC", title: "Previous day's market close price" },
      filled_price: { label: "Filled price" },
      market_value: { label: "Market value", sortKey: "market_value" },
      net_liq: { label: "Net Liq", sortKey: "net_liq", title: "Live liquidation value = price × quantity × contract multiplier (signed; short = negative)" },
      tp: { label: "TP" },
      sl: { label: "SL" },
      submitted_at: { label: "Submitted at" },
      filled_at: { label: "Filled at" },
      time_taken: { label: "Time Taken to Filled" },
      expires: { label: "Expires in Days", sortKey: "expires" },
    };

    // Must equal the number of <Th> cells — and the number of <td> in a data
    // row — because it sizes BOTH the loading skeleton and the empty-state
    // row. It had drifted to 21 against 18 real columns, so the skeleton
    // rendered three phantom cells wider than the header. Channel is
    // conditional, so this is too.
    const COLSPAN = cols.columns.length;

    return (
      <div className={`${className ?? ""} ${fillHeight ? "flex flex-col min-h-0" : ""}`.trim()}>
        {/* A broker we could not read. Without this the table just renders
            fewer rows — indistinguishable from holding nothing, which is what
            made a transient Webull 429 look like a vanished portfolio. */}
        {unreachable.length > 0 && (
          <div
            role="status"
            className="mb-3 flex items-start gap-2 rounded-lg px-3 py-2.5 text-sm"
            style={{
              background: "var(--warn-soft, var(--panel-2))",
              color: "var(--text)",
              border: "1px solid var(--border)",
            }}
          >
            <AlertTriangle size={16} style={{ color: "var(--warn, var(--muted))", flexShrink: 0, marginTop: 1 }} />
            <div className="flex flex-col gap-0.5">
              {unreachable.map((u) => (
                <div key={u.broker_account_id}>
                  <span style={{ fontWeight: 600, textTransform: "capitalize" }}>
                    {u.label || u.broker}
                  </span>
                  <span style={{ color: "var(--muted)" }}>{" — "}{u.detail}.</span>
                  <span style={{ color: "var(--muted)" }}>
                    {" "}Positions from this account are not shown below.
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
        {/* Summary strip */}
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-2.5 mb-4">
          <SummaryTile label="P&L · Today"
            tone={todayRealized == null ? "neutral" : todayRealized > 0 ? "good" : todayRealized < 0 ? "bad" : "neutral"}
            node={todayRealized == null
              ? <span className="num" style={{ color: "var(--muted)" }}>—</span>
              : <AnimatedNumber value={todayRealized} format={fmtSignedUsd} className="num" />}
            sub="Matches Calendar" />
          <LiveTotalsTiles positions={visible} baselinePnl={summary.pnl} baselineMv={summary.mv}
            mvSub={filter === "all" ? "All instruments" : filter === "option" ? "Options" : "Stocks"} />
          <SummaryTile label="Account value" tone="neutral"
            node={totalEquity == null
              ? <span className="num" style={{ color: "var(--muted)" }}>—</span>
              : <AnimatedNumber value={totalEquity} format={fmtUsd} className="num" />}
            sub="Total equity" />
          <SummaryTile label="Long / Short" tone="neutral"
            node={<span className="num">{summary.longs} / {summary.shorts}</span>}
            sub="Direction split" />
        </div>

        {/* Toolbar: type tabs + symbol search */}
        <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            {tabBtn("all", "All")}
            {tabBtn("option", "Options")}
            {tabBtn("stock", "Stocks")}
          </div>
          <div className="flex items-center gap-2">
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
            {/* Show/hide + drag-reorder columns; drag a header edge to resize. */}
            <ColumnsMenu cols={cols} />
          </div>
        </div>

        <div className={`card overflow-hidden ${fillHeight ? "flex flex-col flex-1 min-h-0" : ""}`.trim()} style={{ borderRadius: 10 }}>
          <div className={`overflow-auto ${fillHeight ? "flex-1 min-h-0" : ""}`.trim()}
            role="region" aria-label="Open positions" tabIndex={0}>
            <table className={`min-w-full text-sm ${!loading && visible.length === 0 ? "h-full" : ""}`}>
              <thead className="sticky top-0 z-10" style={{ background: "var(--panel)", boxShadow: "0 1px 0 var(--border)" }}>
                <tr>
                  {cols.columns.map((c) => {
                    const m = HEADER_META[c.id] ?? { label: c.header };
                    return <Th key={c.id} col={c} label={m.label} sortKey={m.sortKey} title={m.title} />;
                  })}
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
                          {positions.length === 0 && unreachable.length > 0
                            ? "Could not load positions from every broker"
                            : positions.length === 0
                            ? "No open positions"
                            : search
                              ? `No positions match “${search}”`
                              : filter === "option" ? "No open option positions"
                                : "No open stock positions"}
                        </div>
                        <div className="text-xs">
                          {positions.length === 0 && unreachable.length > 0
                            ? "This is a broker connection problem, not an empty account — see above."
                            : "Positions appear here once your orders fill."}
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
                {!loading && visible.map(p => {
                  const key = posKey(p);
                  const qtyNum = Number(p.quantity);
                  const isLong = qtyNum > 0;
                  // Live-price key: the ticker for stocks, the OCC for options
                  // (both streamed into the same cache). Options carry the x100
                  // contract multiplier for the value/P&L deltas.
                  const liveSym = p.instrument_type === "stock" ? p.symbol : positionOcc(p);
                  const liveMult = p.instrument_type === "option" ? 100 : 1;
                  const inFlight = closing?.key === key;
                  const mMode: ExitMode = marketMode[key] ?? "close";
                  const lMode: ExitMode = limitMode[key] ?? "close";
                  // Entry-order match for this position — drives Filled price,
                  // the bracket cells and the timestamps. Bracket modify is
                  // allowed while the position is alive (i.e. this row exists);
                  // without a match (broker connected after the trade) the
                  // bracket cells render read-only. The % anchor mirrors Order
                  // History: a copied mirror reverses off filled_avg_price, the
                  // trader's own entry off limit_price.
                  const t = orderTimestamps.byKey.get(orderTimestamps.key(
                    p.broker_account_id, p.instrument_type, p.symbol,
                    p.option_expiry, p.option_strike, p.option_right,
                  ));
                  const orderId = t?.order_id ?? null;
                  const entryPrice = t?.parent_order_id
                    ? (t?.filled_avg_price ?? t?.limit_price ?? null)
                    : (t?.limit_price ?? t?.filled_avg_price ?? null);
                  const isMirror = !!t?.parent_order_id;
                  const tpPct = isMirror && t?.take_profit_pct != null ? Number(t.take_profit_pct) : null;
                  const slPct = isMirror && t?.stop_loss_pct != null ? Number(t.stop_loss_pct) : null;
                  const side = t?.side ?? (isLong ? "buy" : "sell");
                  const onUpdated = (updated: Order) => {
                    setOrders(cur => cur.map(o => o.id === updated.id ? updated : o));
                  };
                  const sub = t?.submitted_at ?? null;
                  const fill = t?.filled_at ?? null;
                  const exp = p.instrument_type === "option" ? fmtExpiresIn(p.option_expiry) : null;

                  // Each column's cell keyed by id; rendered below in the user's
                  // configured order (reorderable) and visibility (show/hide).
                  const cell: Record<string, React.ReactNode> = {
                    channel: (
                      <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: p.discord_channel ? "var(--text)" : "var(--muted)" }} title={p.discord_channel ?? undefined}>
                        {p.discord_channel || "—"}
                      </td>
                    ),
                    symbol: (
                      <td className="px-5 py-3.5 whitespace-nowrap font-medium" style={{ color: "var(--text)" }}>
                        <span className="inline-flex items-center gap-1.5">
                          <PositionIcon kind={positionKind(p)} />
                          {positionSymbolLabel(p)}
                        </span>
                      </td>
                    ),
                    qty: <td className="px-5 py-3.5 num">{fmtNum(String(Math.abs(qtyNum)), 0)}</td>,
                    side: (
                      <td className="px-5 py-3.5">
                        <span className="chip uppercase font-semibold" style={{ background: isLong ? "var(--good-soft)" : "var(--bad-soft)", color: isLong ? "var(--good)" : "var(--bad)", borderColor: "transparent" }}>
                          {isLong ? "Long" : "Short"}
                        </span>
                      </td>
                    ),
                    close_pct: (
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
                                title={disabled ? "Too small at this %" : `${pct}% of the position (×${computedQty}) — sizes a close or an average`}
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
                    ),
                    actions: (
                      <td className="px-5 py-3.5">
                        <div className="flex gap-2 items-center whitespace-nowrap">
                          <div className="flex items-stretch" style={{ width: ACTION_SLOT_W }}>
                            <button
                              disabled={inFlight}
                              onClick={() => (mMode === "average" ? averagePosition(p, "market") : closePosition(p, "market"))}
                              className="btn-ghost flex-1 min-w-0 px-1 py-1 text-xs inline-flex items-center justify-center gap-1"
                              style={{
                                borderTopRightRadius: 0, borderBottomRightRadius: 0,
                                color: mMode === "average" ? "var(--good)" : undefined,
                              }}
                            >
                              <span>{mMode === "average" ? "Avg. at Market" : "Close at Market"}</span>
                              {inFlight && closing.kind === "market" && <Spinner />}
                            </button>
                            <ModeCaret
                              variant="ghost"
                              mode={mMode}
                              labels={{ close: "Close at Market", average: "Avg. at Market" }}
                              onChange={m => setMarketMode(s => ({ ...s, [key]: m }))}
                              disabled={inFlight}
                              actions={[{ label: "Canc.Open Ord", onClick: () => void cancelPositionOpenOrders(p), danger: true }]}
                            />
                          </div>
                          <div className="flex items-stretch">
                            <input
                              type="number" step="0.01" min="0.01"
                              placeholder="Limit"
                              aria-label={`Limit price for ${p.symbol.toUpperCase()}`}
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
                              onClick={() => (lMode === "average" ? averagePosition(p, "limit") : closePosition(p, "limit"))}
                              className="btn-accent-solid px-2 py-1 text-xs font-medium inline-flex items-center justify-center gap-1"
                              style={{
                                borderRadius: 0,
                                minWidth: 40,
                                background: lMode === "average" ? "var(--good)" : undefined,
                              }}
                            >
                              <span>{lMode === "average" ? "Avg." : "Close"}</span>
                              {inFlight && closing.kind === "limit" && <Spinner />}
                            </button>
                            <ModeCaret
                              variant="solid"
                              mode={lMode}
                              labels={{ close: "Close", average: "Avg." }}
                              onChange={m => setLimitMode(s => ({ ...s, [key]: m }))}
                              disabled={inFlight}
                            />
                          </div>
                          <button
                            type="button"
                            onClick={() => setExpanded(s => ({ ...s, [key]: !s[key] }))}
                            aria-expanded={!!expanded[key]}
                            aria-label={expanded[key] ? "Hide stop options" : "Show stop options"}
                            title={expanded[key] ? "Hide stop options" : "Stops and trailing stop"}
                            className="btn-ghost px-1.5 py-1 inline-flex items-center"
                          >
                            {expanded[key] ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                          </button>
                        </div>
                      </td>
                    ),
                    unrealized_pnl: <LivePnlCell symbol={liveSym} snapshotPrice={p.current_price} quantity={p.quantity} baseline={p.unrealized_pnl} multiplier={liveMult} />,
                    pnl_pct: <LivePnlPctCell symbol={liveSym} snapshotPrice={p.current_price} quantity={p.quantity} unrealizedBaseline={p.unrealized_pnl} costBasis={p.cost_basis} multiplier={liveMult} />,
                    // Day's P&L — the broker's OWN per-position figure (Webull /
                    // Alpaca native), not derived. A snapshot value; null → "—".
                    day_pnl: (
                      <td className="px-5 py-3.5 num" title={p.day_pnl_source === "broker_native" ? "From your broker" : undefined}>
                        {p.day_pnl != null ? (
                          <span style={{ color: Number(p.day_pnl) > 0 ? "var(--pnl-pos)" : Number(p.day_pnl) < 0 ? "var(--pnl-neg)" : "var(--text-2)" }}>
                            {fmtSignedUsd(Number(p.day_pnl))}
                            {p.day_pnl_pct != null && (
                              <span style={{ color: "var(--text-2)" }}>{` (${Number(p.day_pnl_pct) >= 0 ? "+" : ""}${Number(p.day_pnl_pct).toFixed(2)}%)`}</span>
                            )}
                          </span>
                        ) : <span style={{ color: "var(--faint)" }}>—</span>}
                      </td>
                    ),
                    avg_entry: <td className="px-5 py-3.5 num">{fmtNum(p.avg_entry_price, 2)}</td>,
                    current_price: <LiveCurrentPriceCell symbol={liveSym} fallback={p.current_price} />,
                    pdc: <td className="px-5 py-3.5 num" style={{ color: "var(--text-2)" }} title="Previous market close price">{fmtNum(p.reference_price, 2)}</td>,
                    filled_price: (
                      <td className="px-5 py-3.5 num">
                        {t?.filled_avg_price ? fmtNum(t.filled_avg_price, 2) : <span style={{ color: "var(--faint)" }}>—</span>}
                      </td>
                    ),
                    market_value: <LiveMarketValueCell symbol={liveSym} snapshotPrice={p.current_price} quantity={p.quantity} baseline={p.market_value} multiplier={liveMult} />,
                    net_liq: <LiveNetLiqCell symbol={liveSym} snapshotPrice={p.current_price} quantity={p.quantity} multiplier={liveMult} />,
                    tp: (
                      <td className="px-5 py-3.5 num">
                        <InlineBracketCell orderId={orderId} leg="tp" value={t?.take_profit_price ?? null} entryPrice={entryPrice} side={side} canEdit={!!orderId} pctOverride={tpPct} onUpdated={onUpdated} />
                      </td>
                    ),
                    sl: (
                      <td className="px-5 py-3.5 num">
                        <InlineBracketCell orderId={orderId} leg="sl" value={t?.stop_loss_price ?? null} entryPrice={entryPrice} side={side} canEdit={!!orderId} pctOverride={slPct} onUpdated={onUpdated} />
                      </td>
                    ),
                    submitted_at: (
                      <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: "var(--muted)" }}>
                        {sub ? fmtDateTimeMs(sub, "America/New_York") : <span style={{ color: "var(--faint)" }}>—</span>}
                      </td>
                    ),
                    filled_at: (
                      <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: "var(--muted)" }}>
                        {fill ? fmtDateTimeMs(fill, "America/New_York") : <span style={{ color: "var(--faint)" }}>—</span>}
                      </td>
                    ),
                    time_taken: (
                      <td className="px-5 py-3.5 whitespace-nowrap num" style={{ color: fill && sub ? "var(--text-2)" : "var(--faint)" }}>
                        {sub && fill ? fmtDuration(sub, fill) : "—"}
                      </td>
                    ),
                    expires: (
                      <td className="px-5 py-3.5 whitespace-nowrap" style={{ color: exp ? exp.color : "var(--faint)" }}>
                        {exp ? exp.text : "—"}
                      </td>
                    ),
                  };
                  return (
                    <Fragment key={key}>
                      <tr className="border-t transition-colors hover:bg-[var(--panel-2)]" style={{ borderColor: "var(--border)" }}>
                        {cols.columns.map((c) => <Fragment key={c.id}>{cell[c.id] ?? null}</Fragment>)}
                      </tr>
                      {expanded[key] && (
                        <PositionStopRow
                          columnIds={cols.columns.map(c => c.id)}
                          orderId={orderId}
                          hasStop={t?.stop_loss_price != null}
                          ladderStop={p.ladder_stop_price ?? null}
                          entryPrice={p.avg_entry_price != null ? Number(p.avg_entry_price) : null}
                          label={p.symbol.toUpperCase()}
                          brokerSymbol={p.broker_symbol}
                          brokerAccountId={p.broker_account_id}
                          onDone={refresh}
                        />
                      )}
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
