"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { ConfirmModal } from "@/components/ConfirmModal";
import { ExitAllModal, type ExitAction } from "@/components/ExitAllModal";
import { OpenPositionsTable, type OpenPositionsTableHandle } from "@/components/OpenPositionsTable";
import type { User } from "@/lib/types";

export default function PositionsPage() {
  const tableRef = useRef<OpenPositionsTableHandle>(null);
  const [user, setUser] = useState<User | null>(null);
  const [exitBusy, setExitBusy] = useState(false);
  const [exitModalOpen, setExitModalOpen] = useState(false);

  useEffect(() => {
    api<User>("/api/auth/me").then(setUser).catch(() => {});
  }, []);

  /** Routes to the right backend endpoint depending on what the user
   *  picked in step 1 of the modal. `action` is fixed to "positions"
   *  for the subscriber path because they don't see the modal that
   *  offers a choice — keep that arg required so a future caller
   *  can't silently drop it. */
  async function runExitAll(action: ExitAction, includeSubscribers: boolean) {
    setExitBusy(true);
    try {
      if (action === "orders") {
        const res = await api<{ cancelled_count: number; failed_count: number }>(
          `/api/trades/cancel-all-open?include_subscribers=${includeSubscribers}`,
          { method: "POST" },
        );
        if (res.cancelled_count === 0 && res.failed_count === 0) {
          notify.info("No open orders to cancel.");
        } else if (res.failed_count === 0) {
          notify.success(
            `Cancelled ${res.cancelled_count} order${res.cancelled_count === 1 ? "" : "s"}` +
            (includeSubscribers && user?.role === "trader" ? " (cascaded to subscriber mirrors)" : "")
          );
        } else {
          notify.warn(`Cancelled ${res.cancelled_count}; ${res.failed_count} failed — check Order History for details`);
        }
      } else {
        const res = await api<{ closed_count: number; failed_count: number }>(
          `/api/positions/close-all?include_subscribers=${includeSubscribers}`,
          { method: "POST" },
        );
        if (res.closed_count === 0 && res.failed_count === 0) {
          notify.info("No open positions to close.");
        } else if (res.failed_count === 0) {
          notify.success(`Exited ${res.closed_count} position${res.closed_count === 1 ? "" : "s"} at market${includeSubscribers && user?.role === "trader" ? " (fanned out to subscribers)" : ""}`);
        } else {
          notify.warn(`Exited ${res.closed_count}; ${res.failed_count} failed — check Order History for details`);
        }
      }
      tableRef.current?.refresh();
      setExitModalOpen(false);
    } catch (e) {
      notify.fromError(e, "Exit all failed");
    } finally {
      setExitBusy(false);
    }
  }

  const isTrader = user?.role === "trader";

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <h1 className="text-2xl font-semibold">Positions</h1>
        <button
          type="button"
          onClick={() => setExitModalOpen(true)}
          disabled={exitBusy}
          title="Close every open position at market across all connected brokers"
          className="btn-danger-soft shrink-0 px-3 py-2 text-sm font-medium inline-flex items-center gap-2"
        >
          <span>Exit All</span>
          {exitBusy && <Spinner />}
        </button>
      </div>
      <OpenPositionsTable ref={tableRef} />

      {/* Trader: three-step modal (action → scope → confirm-fanout). */}
      {isTrader && (
        <ExitAllModal
          open={exitModalOpen}
          busy={exitBusy}
          onConfirm={(action, includeSubs) => runExitAll(action, includeSubs)}
          onCancel={() => setExitModalOpen(false)}
        />
      )}

      {/* Subscriber: simpler single-confirm — no fanout to choose, but
          they still see the action question implicitly because we
          default to closing positions (the more common subscriber
          intent). If we ever want subscribers to bulk-cancel orders
          too, swap this for the trader's ExitAllModal sans the scope
          step. */}
      {!isTrader && (
        <ConfirmModal
          open={exitModalOpen}
          title="Exit all positions?"
          message="This will close every open position at market across every connected broker. The action cannot be undone."
          confirmLabel="Exit all at market"
          variant="danger"
          busy={exitBusy}
          onConfirm={() => runExitAll("positions", false)}
          onCancel={() => setExitModalOpen(false)}
        />
      )}
    </div>
  );
}
