"use client";

/**
 * BulkExitBar — the glass-card strip that surfaces the four bulk-exit
 * actions (close my positions, cancel my orders, plus the two trader-only
 * subscribers variants). Used above the OpenPositionsTable on both the
 * Trade Panel and the /positions page so the action set stays in sync.
 *
 * Owns its own state:
 *  - fetches the current user to gate the two trader-only chips,
 *  - tracks `pending` so the ConfirmModal can hang off a single slot,
 *  - drives the HTTP + toast plumbing internally — callers just pass an
 *    `onActionComplete` hook (typically a table-refresh) and forget.
 */
import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { ConfirmModal } from "@/components/ConfirmModal";
import type { User } from "@/lib/types";

type ExitKey = "my_positions" | "my_orders" | "subs_positions" | "subs_orders";

interface ExitDef {
  label: string;
  title: string;
  message: string;
  confirmLabel: string;
  /** Subscriber-targeted (trader-only) — gets the red gradient. */
  subs: boolean;
  /** SVG path data. Each path segment can be a separate `d=` string,
   *  joined by spaces with a leading `M`; we split on " M" at render time
   *  to draw them as separate `<path>` elements so they render correctly. */
  iconPath: string;
}

const EXIT_DEFS: Record<ExitKey, ExitDef> = {
  my_positions: {
    label: "Exit My Positions",
    title: "Exit all your positions?",
    message:
      "Places a market order to close every open position in YOUR connected brokers. Subscribers are not affected. This cannot be undone.",
    confirmLabel: "Exit my positions",
    subs: false,
    iconPath: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4 M16 17l5-5-5-5 M21 12H9",
  },
  my_orders: {
    label: "Cancel My Orders",
    title: "Cancel all your open orders?",
    message:
      "Cancels every still-working order in YOUR connected brokers (Pending / Submitted / Accepted / Partially Filled). Subscribers' orders are not affected. This cannot be undone.",
    confirmLabel: "Cancel my orders",
    subs: false,
    iconPath:
      "M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M15 9l-6 6 M9 9l6 6",
  },
  subs_positions: {
    label: "Exit Subscribers Positions",
    title: "Exit ALL subscribers' positions?",
    message:
      "Places market orders to close every open position across EVERY subscriber's broker accounts. Your own positions are NOT touched. This cannot be undone.",
    confirmLabel: "Exit subscribers' positions",
    subs: true,
    iconPath:
      "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M22 11l-3 3-3-3",
  },
  subs_orders: {
    label: "Cancel Subscribers Orders",
    title: "Cancel ALL subscribers' open orders?",
    message:
      "Cancels every still-working order across EVERY subscriber's broker accounts. Your own orders are NOT touched. This cannot be undone.",
    confirmLabel: "Cancel subscribers' orders",
    subs: true,
    iconPath:
      "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M19 8a3 3 0 1 1 0 6 3 3 0 0 1 0-6z M17 7l4 4",
  },
};

// Theme-aware surface: was a hardcoded dark glass gradient that looked wrong
// (dark box) in light mode. Use the panel token so it adapts to both themes.
const cardStyle: React.CSSProperties = {
  background: "var(--panel)",
  border: "1px solid var(--border)",
  borderRadius: 10,
};

interface Props {
  /** Called after a bulk action completes successfully — typically a
   *  `tableRef.current?.refresh()` so the positions list re-renders. */
  onActionComplete?: () => void;
}

export function BulkExitBar({ onActionComplete }: Props) {
  const [user, setUser] = useState<User | null>(null);
  const [pending, setPending] = useState<ExitKey | null>(null);
  const [busy, setBusy] = useState(false);
  // Optional trailing-stop trail (%) for "Exit My Positions". Empty = market
  // close (the classic behaviour). When set, stock positions on brokers that
  // support trailing stops close as a TRAILING_STOP; options / unsupported
  // brokers fall back to market. See services/trailing_stop_close.
  const [trailPct, setTrailPct] = useState("");
  const trailNum = parseFloat(trailPct);
  const useTrail = !isNaN(trailNum) && trailNum > 0 && trailNum <= 100;

  // Sell-All snapshot + re-entry. After a Sell-All the positions are saved so
  // they can be re-opened. Each item carries a live re-entry status
  // (filled / working / pending); Re-Enter only acts on the pending ones.
  type SnapSummary = { total: number; filled: number; working: number; pending: number };
  const [snapshot, setSnapshot] = useState<{ id: string; summary: SnapSummary } | null>(null);
  const [reDiscount, setReDiscount] = useState("");
  const [reBusy, setReBusy] = useState(false);

  async function loadSnapshot() {
    try {
      const r = await api<{ snapshot: { id: string; summary: SnapSummary } | null }>(
        "/api/positions/snapshots/latest",
      );
      setSnapshot(r.snapshot);
    } catch { /* no snapshot yet */ }
  }

  useEffect(() => {
    api<User>("/api/auth/me").then(setUser).catch(() => {});
    loadSnapshot();
  }, []);

  // While a snapshot with unresolved items is shown, poll so fills flip
  // pending/working → filled live (30s cadence, cheap).
  useEffect(() => {
    if (!snapshot || snapshot.summary.pending + snapshot.summary.working === 0) return;
    const id = setInterval(loadSnapshot, 15_000);
    return () => clearInterval(id);
  }, [snapshot]);

  async function reEnter() {
    const d = parseFloat(reDiscount);
    const useD = !isNaN(d) && d > 0 && d <= 100;
    setReBusy(true);
    try {
      const res = await api<{ placed_count: number; skipped_count: number; failed_count: number }>(
        `/api/positions/re-enter${useD ? `?discount_percent=${d}` : ""}`,
        { method: "POST" },
      );
      const skip = res.skipped_count ? `, ${res.skipped_count} already in/resting` : "";
      if (res.placed_count === 0 && res.failed_count === 0)
        notify.info(res.skipped_count ? `Nothing new to re-enter${skip}.` : "Nothing to re-enter.");
      else if (res.failed_count === 0)
        notify.success(
          `Re-entered ${res.placed_count} position${res.placed_count === 1 ? "" : "s"}` +
          (useD ? ` — limit ${d}% below exit` : " at market") + `${skip}.`,
        );
      else notify.warn(`Re-entered ${res.placed_count}; ${res.failed_count} failed${skip} — check Order History.`);
      onActionComplete?.();
      loadSnapshot();  // refresh per-item status
    } catch (e) {
      notify.fromError(e, "Re-enter failed");
    } finally {
      setReBusy(false);
    }
  }

  async function runExit(key: ExitKey) {
    if (key === "my_positions") {
      const url = "/api/positions/close-all?include_subscribers=false"
        + (useTrail ? `&trail_percent=${trailNum}` : "");
      const res = await api<{ closed: { method?: string }[]; closed_count: number; failed_count: number }>(
        url, { method: "POST" },
      );
      const nTrail = (res.closed ?? []).filter(c => c.method === "trailing_stop").length;
      if (res.closed_count === 0 && res.failed_count === 0) notify.info("No open positions to close (yours).");
      else if (res.failed_count === 0)
        notify.success(
          useTrail && nTrail > 0
            ? `Exited ${res.closed_count} — ${nTrail} as trailing stop (${trailNum}%).`
            : `Exited ${res.closed_count} position${res.closed_count === 1 ? "" : "s"} at market — yours.`,
        );
      else notify.warn(`Exited ${res.closed_count}; ${res.failed_count} failed — check Order History.`);
    } else if (key === "my_orders") {
      const res = await api<{ cancelled_count: number; failed_count: number }>(
        "/api/trades/cancel-all-open?include_subscribers=false",
        { method: "POST" },
      );
      if (res.cancelled_count === 0 && res.failed_count === 0) notify.info("No open orders to cancel (yours).");
      else if (res.failed_count === 0) notify.success(`Cancelled ${res.cancelled_count} order${res.cancelled_count === 1 ? "" : "s"} — yours.`);
      else notify.warn(`Cancelled ${res.cancelled_count}; ${res.failed_count} failed — check Order History.`);
    } else if (key === "subs_positions") {
      // Async/background: API returns immediately with a queued count;
      // closes stream in over the next ~30-120s and update the UI via
      // SSE order.placed events.
      const res = await api<{ queued_pairs: number; message: string }>(
        "/api/positions/close-all-subscribers",
        { method: "POST" },
      );
      if (res.queued_pairs === 0) notify.info(res.message ?? "No subscriber positions to close.");
      else notify.success(res.message ?? `Queued close-positions sweep across ${res.queued_pairs} accounts.`);
    } else if (key === "subs_orders") {
      // Same background pattern. With 1,000+ open subscriber orders the
      // sweep can take several minutes; the UI listens for per-order
      // SSE order.cancelled events so Order History updates live.
      const res = await api<{ queued_count: number; message: string }>(
        "/api/trades/cancel-all-subscribers-open",
        { method: "POST" },
      );
      if (res.queued_count === 0) notify.info(res.message ?? "No subscriber orders to cancel.");
      else notify.success(res.message ?? `Queued ${res.queued_count} cancellations — see Order History.`);
    }
  }

  async function confirmRun() {
    if (!pending) return;
    setBusy(true);
    try {
      await runExit(pending);
      onActionComplete?.();
      loadSnapshot();  // a Sell-All just saved a new snapshot
      setPending(null);
    } catch (e) {
      notify.fromError(e, "Action failed");
    } finally {
      setBusy(false);
    }
  }

  const isTrader = user?.role === "trader";
  const keys: ExitKey[] = isTrader
    ? ["my_positions", "my_orders", "subs_positions", "subs_orders"]
    : ["my_positions", "my_orders"];

  return (
    <>
      <div
        className="rounded-xl px-3 py-2.5 flex items-center justify-between gap-3 flex-wrap"
        style={cardStyle}
      >
        <div className="flex items-center gap-2 shrink-0">
          <span className="w-1.5 h-1.5 rounded-full" style={{ background: "var(--bad)" }} />
          <span
            className="text-[10px] uppercase tracking-[0.2em] font-semibold"
            style={{ color: "var(--text-2)" }}
          >
            Bulk Exit
          </span>
        </div>
        <div className="flex flex-wrap gap-2 justify-end items-center">
          {/* Trailing-stop trail for Exit My Positions. Leave empty for a
              market exit; enter e.g. 5 to close as a trailing stop where the
              broker supports it. */}
          <div
            className="inline-flex items-center gap-1.5 px-2 py-1 rounded-lg"
            style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}
            title="Optional: close Exit My Positions as a trailing stop at this % (stocks on supported brokers). Empty = market exit."
          >
            <span className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: "var(--text-2)" }}>
              Trail&nbsp;%
            </span>
            <input
              type="number" min="0" max="100" step="0.5"
              value={trailPct}
              onChange={e => setTrailPct(e.target.value)}
              placeholder="off"
              aria-label="Trailing stop percent for Exit My Positions"
              className="w-14 text-xs rounded-md px-1.5 py-0.5 outline-none"
              style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--text)" }}
            />
          </div>
          {keys.map(key => {
            const def = EXIT_DEFS[key];
            const isSubs = def.subs;
            return (
              <button
                key={key}
                type="button"
                onClick={() => setPending(key)}
                disabled={busy}
                title={def.message}
                className="group inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                style={{
                  // Subs (destructive-to-subscribers) keep the red tint — it
                  // reads on both themes. Non-subs use the elevated panel token
                  // so they're a visible pill on both light and dark (the old
                  // white overlay vanished on a light card).
                  background: isSubs
                    ? "linear-gradient(180deg, rgba(239,68,68,0.18), rgba(239,68,68,0.06))"
                    : "var(--panel-2)",
                  border: `1px solid ${isSubs ? "rgba(239,68,68,0.35)" : "var(--border)"}`,
                  color: isSubs ? "var(--bad)" : "var(--text)",
                }}
                onMouseEnter={e => {
                  e.currentTarget.style.background = isSubs
                    ? "linear-gradient(180deg, rgba(239,68,68,0.28), rgba(239,68,68,0.10))"
                    : "var(--accent-glow)";
                }}
                onMouseLeave={e => {
                  e.currentTarget.style.background = isSubs
                    ? "linear-gradient(180deg, rgba(239,68,68,0.18), rgba(239,68,68,0.06))"
                    : "var(--panel-2)";
                }}
              >
                <svg
                  width="13"
                  height="13"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden
                >
                  {def.iconPath.split(" M").map((seg, i) => (
                    <path key={i} d={i === 0 ? seg : `M${seg}`} />
                  ))}
                </svg>
                <span>{def.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {snapshot && snapshot.summary.total > 0 && (
        <div
          className="rounded-xl px-3 py-2.5 flex items-center justify-between gap-3 flex-wrap mt-2"
          style={cardStyle}
        >
          <div className="flex items-center gap-2 shrink-0 flex-wrap">
            <span className="w-1.5 h-1.5 rounded-full" style={{ background: "var(--good)" }} />
            <span className="text-[10px] uppercase tracking-[0.2em] font-semibold" style={{ color: "var(--text-2)" }}>
              Re-Enter Last Exit
            </span>
            {/* Fill-aware status: how many of the saved positions are back. */}
            <span className="text-xs" style={{ color: "var(--good)" }}>
              {snapshot.summary.filled}/{snapshot.summary.total} back in
            </span>
            {snapshot.summary.working > 0 && (
              <span className="text-xs" style={{ color: "#facc15" }}>· {snapshot.summary.working} resting</span>
            )}
            {snapshot.summary.pending > 0 && (
              <span className="text-xs" style={{ color: "var(--muted)" }}>· {snapshot.summary.pending} to go</span>
            )}
            {/* Jump to the full per-order Snapshot page. */}
            <Link href="/snapshot" className="text-xs font-medium" style={{ color: "var(--accent)" }}>
              View snapshot →
            </Link>
          </div>
          <div className="flex items-center gap-2">
            <div
              className="inline-flex items-center gap-1.5 px-2 py-1 rounded-lg"
              style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}
              title="Optional: re-buy each position this % BELOW its exit price (a resting limit). Empty = buy back now at market."
            >
              <span className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: "var(--text-2)" }}>
                %&nbsp;below
              </span>
              <input
                type="number" min="0" max="100" step="0.5"
                value={reDiscount}
                onChange={e => setReDiscount(e.target.value)}
                placeholder="mkt"
                aria-label="Re-enter discount percent below exit price"
                className="w-14 text-xs rounded-md px-1.5 py-0.5 outline-none"
                style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--text)" }}
              />
            </div>
            <button
              type="button"
              onClick={reEnter}
              disabled={reBusy || snapshot.summary.pending === 0}
              title={snapshot.summary.pending === 0
                ? "Nothing to re-enter — all positions are back or have a resting order"
                : `Re-enter the ${snapshot.summary.pending} position(s) not yet back`}
              className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all disabled:opacity-50 disabled:cursor-not-allowed"
              style={{ background: "var(--accent)", color: "var(--accent-ink)", border: "1px solid var(--accent)" }}
            >
              {reBusy ? "Re-entering…"
                : snapshot.summary.pending === 0 ? "All re-entered"
                : `Re-Enter (${snapshot.summary.pending})`}
            </button>
          </div>
        </div>
      )}

      <ConfirmModal
        open={pending !== null}
        title={pending ? EXIT_DEFS[pending].title : ""}
        message={(() => {
          if (pending !== "my_positions") return pending ? EXIT_DEFS[pending].message : "";
          const base = useTrail
            ? `Closes every open position in YOUR connected brokers with a TRAILING STOP (${trailNum}% trail) where the broker supports it (stocks); options and unsupported brokers fall back to a market close. Subscribers are not affected.`
            : EXIT_DEFS.my_positions.message;
          // Warn if the current snapshot still has positions not re-entered —
          // this exit will supersede it and abandon them.
          const nPending = snapshot?.summary.pending ?? 0;
          if (nPending === 0) return base;
          return (
            <>
              <span style={{ color: "var(--bad)", fontWeight: 600 }}>
                ⚠ You still have {nPending} position{nPending === 1 ? "" : "s"} not re-entered from your last
                exit. This new exit will REPLACE that snapshot, and those un-re-entered ones will no longer be
                available to re-enter here.
              </span>
              <div className="mt-2">{base}</div>
            </>
          );
        })()}
        confirmLabel={pending ? EXIT_DEFS[pending].confirmLabel : "Confirm"}
        variant="danger"
        busy={busy}
        onConfirm={confirmRun}
        onCancel={() => { if (!busy) setPending(null); }}
      />
    </>
  );
}
