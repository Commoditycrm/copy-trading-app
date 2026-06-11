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

const cardStyle: React.CSSProperties = {
  background:
    "linear-gradient(180deg, rgba(20,26,32,0.55) 0%, rgba(10,14,18,0.35) 100%)",
  border: "1px solid var(--border)",
  borderRadius: "var(--r)",
  backdropFilter: "blur(10px)",
  WebkitBackdropFilter: "blur(10px)",
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

  useEffect(() => {
    api<User>("/api/auth/me").then(setUser).catch(() => {});
  }, []);

  async function runExit(key: ExitKey) {
    if (key === "my_positions") {
      const res = await api<{ closed_count: number; failed_count: number }>(
        "/api/positions/close-all?include_subscribers=false",
        { method: "POST" },
      );
      if (res.closed_count === 0 && res.failed_count === 0) notify.info("No open positions to close (yours).");
      else if (res.failed_count === 0) notify.success(`Exited ${res.closed_count} position${res.closed_count === 1 ? "" : "s"} at market — yours.`);
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
        <div className="flex flex-wrap gap-2 justify-end">
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
                  background: isSubs
                    ? "linear-gradient(180deg, rgba(239,68,68,0.18), rgba(239,68,68,0.06))"
                    : "linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.02))",
                  border: `1px solid ${isSubs ? "rgba(239,68,68,0.35)" : "var(--border)"}`,
                  color: isSubs ? "var(--bad)" : "var(--text)",
                }}
                onMouseEnter={e => {
                  e.currentTarget.style.background = isSubs
                    ? "linear-gradient(180deg, rgba(239,68,68,0.28), rgba(239,68,68,0.10))"
                    : "linear-gradient(180deg, rgba(255,255,255,0.09), rgba(255,255,255,0.04))";
                }}
                onMouseLeave={e => {
                  e.currentTarget.style.background = isSubs
                    ? "linear-gradient(180deg, rgba(239,68,68,0.18), rgba(239,68,68,0.06))"
                    : "linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.02))";
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

      <ConfirmModal
        open={pending !== null}
        title={pending ? EXIT_DEFS[pending].title : ""}
        message={pending ? EXIT_DEFS[pending].message : ""}
        confirmLabel={pending ? EXIT_DEFS[pending].confirmLabel : "Confirm"}
        variant="danger"
        busy={busy}
        onConfirm={confirmRun}
        onCancel={() => { if (!busy) setPending(null); }}
      />
    </>
  );
}
