"use client";

import { useRef, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { ConfirmModal } from "@/components/ConfirmModal";
import { OpenPositionsTable, type OpenPositionsTableHandle } from "@/components/OpenPositionsTable";

export default function PositionsPage() {
  const tableRef = useRef<OpenPositionsTableHandle>(null);
  const [exitBusy, setExitBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  async function doExitAll() {
    setExitBusy(true);
    try {
      const res = await api<{ closed_count: number; failed_count: number }>(
        "/api/positions/close-all", { method: "POST" }
      );
      if (res.closed_count === 0 && res.failed_count === 0) {
        notify.info("No open positions to close.");
      } else if (res.failed_count === 0) {
        notify.success(`Exited ${res.closed_count} position${res.closed_count === 1 ? "" : "s"} at market`);
      } else {
        notify.warn(`Exited ${res.closed_count}; ${res.failed_count} failed — check Order History for details`);
      }
      tableRef.current?.refresh();
      setConfirmOpen(false);
    } catch (e) {
      notify.fromError(e, "Exit all failed");
    } finally {
      setExitBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <h1 className="text-2xl font-semibold">Positions</h1>
        <button
          type="button"
          onClick={() => setConfirmOpen(true)}
          disabled={exitBusy}
          title="Close every open position at market across all connected brokers"
          className="btn-danger-soft shrink-0 px-3 py-2 text-sm font-medium inline-flex items-center gap-2"
        >
          <span>Exit All</span>
          {exitBusy && <Spinner />}
        </button>
      </div>
      <OpenPositionsTable ref={tableRef} />
      <ConfirmModal
        open={confirmOpen}
        title="Exit all positions?"
        message="This will close every open position at market across every connected broker. The action cannot be undone."
        confirmLabel="Exit all at market"
        variant="danger"
        busy={exitBusy}
        onConfirm={doExitAll}
        onCancel={() => setConfirmOpen(false)}
      />
    </div>
  );
}
