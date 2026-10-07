"use client";

/**
 * Position summary: every fill of one holding, oldest first, with the quantity
 * remaining after it —
 *
 *     Buy  10 @ 5.20   Rem.Qty 10
 *     Sell  5 @ 5.79   Rem.Qty  5
 *
 * Shown in the panel that opens under an open position, and under a sold one
 * in Closed today (``throughOrderId`` = the sell, for the holding it closed).
 * Reads GET /api/positions/history — our own orders, no broker call.
 */
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { fmtDateTimeMs } from "@/lib/format";

type Fill = {
  order_id: string;
  side: "buy" | "sell";
  quantity: string;
  price: string | null;
  at: string | null;
  remaining: string;
};

export type SummaryTarget = {
  brokerAccountId: string;
  symbol: string;
  optionStrike?: string | null;
  optionRight?: string | null;
  optionExpiry?: string | null;
  /** A closing order: show the holding it belongs to rather than the open one. */
  throughOrderId?: string;
};

export function PositionSummary({ target }: { target: SummaryTarget }) {
  const [fills, setFills] = useState<Fill[] | null>(null);
  const [error, setError] = useState(false);

  const q = new URLSearchParams({ broker_account_id: target.brokerAccountId, symbol: target.symbol });
  if (target.optionStrike) q.set("option_strike", target.optionStrike);
  if (target.optionRight) q.set("option_right", target.optionRight);
  if (target.optionExpiry) q.set("option_expiry", target.optionExpiry.slice(0, 10));
  if (target.throughOrderId) q.set("through_order_id", target.throughOrderId);
  const url = `/api/positions/history?${q.toString()}`;

  useEffect(() => {
    let live = true;
    api<Fill[]>(url)
      .then((rows) => { if (live) setFills(rows); })
      .catch(() => { if (live) setError(true); });
    return () => { live = false; };
  }, [url]);

  const muted = { color: "var(--muted)" } as const;
  return (
    <div>
      <div className="text-[10px] font-medium uppercase tracking-wide mb-1.5" style={{ color: "var(--text-2)" }}>
        Position summary
      </div>
      {error ? (
        <div className="text-[12px]" style={muted}>Couldn&apos;t load this position&apos;s fills.</div>
      ) : fills === null ? (
        <div className="text-[12px]" style={muted}>Loading…</div>
      ) : fills.length === 0 ? (
        <div className="text-[12px]" style={muted}>
          No fills placed through Kopyya for this position — it may have been opened in the broker&apos;s own app.
        </div>
      ) : (
        <div className="grid gap-x-4 gap-y-1 text-[12px] items-center"
             style={{ gridTemplateColumns: "auto auto auto auto 1fr" }}>
          <span style={muted}>Time (ET)</span>
          <span style={muted}>Side</span>
          <span className="text-right" style={muted}>Qty @ Price</span>
          <span className="text-right" style={muted}>Rem.Qty</span>
          <span />
          {fills.map((f) => (
            <FillRow key={f.order_id} f={f} />
          ))}
        </div>
      )}
    </div>
  );
}

function FillRow({ f }: { f: Fill }) {
  const buy = f.side === "buy";
  return (
    <>
      <span className="num whitespace-nowrap" style={{ color: "var(--muted)" }}>
        {f.at ? fmtDateTimeMs(f.at, "America/New_York") : "—"}
      </span>
      <span className="font-semibold uppercase text-[11px]" style={{ color: buy ? "var(--good)" : "var(--bad)" }}>
        {buy ? "Buy" : "Sell"}
      </span>
      <span className="num text-right whitespace-nowrap" style={{ color: "var(--text)" }}>
        {f.quantity} @ {f.price ?? "—"}
      </span>
      <span className="num text-right font-semibold" style={{ color: "var(--text)" }}>{f.remaining}</span>
      <span />
    </>
  );
}
