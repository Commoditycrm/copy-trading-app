"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { ConfirmModal } from "@/components/ConfirmModal";
import { OpenPositionsTable, type OpenPositionsTableHandle } from "@/components/OpenPositionsTable";
import type { User } from "@/lib/types";

/** The bulk-exit actions. "My*" act on the caller's own brokers; "subs*" are
 *  trader-only and act on every subscriber's brokers (NOT the trader's own). */
type ExitKind = "my_positions" | "my_orders" | "subs_positions" | "subs_orders";

interface ExitDef {
  label: string;
  title: string;
  message: string;
  confirmLabel: string;
  /** Whether this is a trader-only "subscribers" action (styled louder). */
  subs: boolean;
  run: () => Promise<void>;
}

export default function PositionsPage() {
  const tableRef = useRef<OpenPositionsTableHandle>(null);
  const [user, setUser] = useState<User | null>(null);
  const [exitBusy, setExitBusy] = useState(false);
  const [pending, setPending] = useState<ExitKind | null>(null);

  useEffect(() => {
    api<User>("/api/auth/me").then(setUser).catch(() => {});
  }, []);

  const isTrader = user?.role === "trader";

  async function closeCall(url: string, who: string) {
    const res = await api<{ closed_count: number; failed_count: number }>(url, { method: "POST" });
    if (res.closed_count === 0 && res.failed_count === 0) {
      notify.info(`No open positions to close (${who}).`);
    } else if (res.failed_count === 0) {
      notify.success(`Exited ${res.closed_count} position${res.closed_count === 1 ? "" : "s"} at market — ${who}.`);
    } else {
      notify.warn(`Exited ${res.closed_count}; ${res.failed_count} failed (${who}) — check Order History.`);
    }
  }

  async function cancelCall(url: string, who: string) {
    const res = await api<{ cancelled_count: number; failed_count: number }>(url, { method: "POST" });
    if (res.cancelled_count === 0 && res.failed_count === 0) {
      notify.info(`No open orders to cancel (${who}).`);
    } else if (res.failed_count === 0) {
      notify.success(`Cancelled ${res.cancelled_count} order${res.cancelled_count === 1 ? "" : "s"} — ${who}.`);
    } else {
      notify.warn(`Cancelled ${res.cancelled_count}; ${res.failed_count} failed (${who}) — check Order History.`);
    }
  }

  const DEFS: Record<ExitKind, ExitDef> = {
    my_positions: {
      label: "Exit All My Positions",
      title: "Exit all your positions?",
      message: "This places a market order to close every open position in YOUR connected brokers. Subscribers are not affected. This cannot be undone.",
      confirmLabel: "Exit my positions",
      subs: false,
      run: () => closeCall("/api/positions/close-all?include_subscribers=false", "yours"),
    },
    my_orders: {
      label: "Exit All My Open Orders",
      title: "Cancel all your open orders?",
      message: "This cancels every still-working order in YOUR connected brokers (Pending / Submitted / Accepted / Partially Filled). Subscribers' orders are not affected. This cannot be undone.",
      confirmLabel: "Cancel my orders",
      subs: false,
      run: () => cancelCall("/api/trades/cancel-all-open?include_subscribers=false", "yours"),
    },
    subs_positions: {
      label: "Exit All Subscribers Positions",
      title: "Exit ALL subscribers' positions?",
      message: "This places market orders to close every open position across EVERY subscriber's broker accounts. Your own positions are NOT touched. This cannot be undone.",
      confirmLabel: "Exit subscribers' positions",
      subs: true,
      run: () => closeCall("/api/positions/close-all-subscribers", "all subscribers"),
    },
    subs_orders: {
      label: "Exit All Subscribers Open Orders",
      title: "Cancel ALL subscribers' open orders?",
      message: "This cancels every still-working order across EVERY subscriber's broker accounts. Your own orders are NOT touched. This cannot be undone.",
      confirmLabel: "Cancel subscribers' orders",
      subs: true,
      run: () => cancelCall("/api/trades/cancel-all-subscribers-open", "all subscribers"),
    },
  };

  const kinds: ExitKind[] = isTrader
    ? ["my_positions", "my_orders", "subs_positions", "subs_orders"]
    : ["my_positions", "my_orders"];

  async function confirmRun() {
    if (!pending) return;
    setExitBusy(true);
    try {
      await DEFS[pending].run();
      tableRef.current?.refresh();
      setPending(null);
    } catch (e) {
      notify.fromError(e, "Action failed");
    } finally {
      setExitBusy(false);
    }
  }

  const def = pending ? DEFS[pending] : null;

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <h1 className="text-2xl font-semibold">Positions</h1>
        <div className="flex flex-wrap gap-2 justify-end">
          {kinds.map((k) => {
            const d = DEFS[k];
            return (
              <button
                key={k}
                type="button"
                onClick={() => setPending(k)}
                disabled={exitBusy}
                className="btn-danger-soft shrink-0 px-3 py-2 text-sm font-medium"
              >
                {d.label}
              </button>
            );
          })}
        </div>
      </div>

      <OpenPositionsTable ref={tableRef} />

      <ConfirmModal
        open={pending !== null}
        title={def?.title ?? ""}
        message={def?.message ?? ""}
        confirmLabel={def?.confirmLabel ?? "Confirm"}
        variant="danger"
        busy={exitBusy}
        onConfirm={confirmRun}
        onCancel={() => { if (!exitBusy) setPending(null); }}
      />
    </div>
  );
}
