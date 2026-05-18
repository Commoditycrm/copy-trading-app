"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { ConfirmModal } from "@/components/ConfirmModal";
import { ExitAllModal } from "@/components/ExitAllModal";
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

  async function runExitAll(includeSubscribers: boolean) {
    setExitBusy(true);
    try {
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

      {/* Trader: two-step scope picker + confirmation. */}
      {isTrader && (
        <ExitAllModal
          open={exitModalOpen}
          busy={exitBusy}
          onConfirm={(includeSubs) => runExitAll(includeSubs)}
          onCancel={() => setExitModalOpen(false)}
        />
      )}

      {/* Subscriber: single confirmation — there's no fanout to choose. */}
      {!isTrader && (
        <ConfirmModal
          open={exitModalOpen}
          title="Exit all positions?"
          message="This will close every open position at market across every connected broker. The action cannot be undone."
          confirmLabel="Exit all at market"
          variant="danger"
          busy={exitBusy}
          onConfirm={() => runExitAll(false)}
          onCancel={() => setExitModalOpen(false)}
        />
      )}
    </div>
  );
}
