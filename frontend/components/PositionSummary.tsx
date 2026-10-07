"use client";

/**
 * Position summary: everything that happened to one holding, oldest first —
 *
 *     Entry     4 @ 2.05 limit   filled 4 @ 2.00    Rem.Qty 4
 *     Stop set  @ 1.50
 *     T1        3 @ 2.40 limit   filled 3 @ 2.42    Rem.Qty 1
 *     Trailing stop raised  1.50 → 1.80 · 15% below high 2.12
 *
 * Orders show what was asked for (qty, market / limit price) and what filled;
 * the stop's own history (set, moved, trailing raised, removed) sits between
 * them in time. Shown in the panel under an open position, and under a sold
 * one in Closed today (``throughOrderId`` = the sell, for the holding it
 * closed). Reads GET /api/positions/history — our own records, no broker call.
 */
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { fmtDateTimeMs } from "@/lib/format";

type Item = {
  type: "order" | "event";
  at: string | null;
  label: string;
  side: "buy" | "sell" | null;
  requested: string | null;
  filled: string | null;
  status: string | null;
  remaining: string | null;
  detail: string | null;
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
  const [items, setItems] = useState<Item[] | null>(null);
  const [error, setError] = useState(false);

  const q = new URLSearchParams({ broker_account_id: target.brokerAccountId, symbol: target.symbol });
  if (target.optionStrike) q.set("option_strike", target.optionStrike);
  if (target.optionRight) q.set("option_right", target.optionRight);
  if (target.optionExpiry) q.set("option_expiry", target.optionExpiry.slice(0, 10));
  if (target.throughOrderId) q.set("through_order_id", target.throughOrderId);
  const url = `/api/positions/history?${q.toString()}`;

  useEffect(() => {
    let live = true;
    api<Item[]>(url)
      .then((rows) => { if (live) setItems(rows); })
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
        <div className="text-[12px]" style={muted}>Couldn&apos;t load this position&apos;s history.</div>
      ) : items === null ? (
        <div className="text-[12px]" style={muted}>Loading…</div>
      ) : items.length === 0 ? (
        <div className="text-[12px]" style={muted}>
          Nothing placed through Kopyya for this position — it may have been opened in the broker&apos;s own app.
        </div>
      ) : (
        <div className="grid gap-x-4 gap-y-1 text-[12px] items-center"
             style={{ gridTemplateColumns: "auto auto auto auto auto 1fr" }}>
          <span style={muted}>Time (ET)</span>
          <span style={muted}>Event</span>
          <span style={muted}>Requested</span>
          <span style={muted}>Filled</span>
          <span className="text-right" style={muted}>Rem.Qty</span>
          <span />
          {items.map((it, i) => <Row key={i} it={it} />)}
        </div>
      )}
    </div>
  );
}

/** How an order that has not filled reads in the Filled column. */
const UNFILLED: Record<string, string> = {
  pending: "waiting", submitted: "resting", accepted: "resting",
  partially_filled: "part filled", rejected: "rejected",
};

function Row({ it }: { it: Item }) {
  const time = (
    <span className="num whitespace-nowrap" style={{ color: "var(--muted)" }}>
      {it.at ? fmtDateTimeMs(it.at, "America/New_York") : "—"}
    </span>
  );
  if (it.type === "event") {
    return (
      <>
        {time}
        <span className="whitespace-nowrap" style={{ color: "var(--text-2)" }}>{it.label}</span>
        <span className="num" style={{ gridColumn: "span 2", color: "var(--text-2)" }}>{it.detail}</span>
        <span />
        <span />
      </>
    );
  }
  const buy = it.side === "buy";
  // Part of the holding's fills (it has a Rem.Qty) — else an order that
  // hasn't traded, shown by its status.
  const filled = it.remaining != null;
  return (
    <>
      {time}
      <span className="font-semibold whitespace-nowrap" style={{ color: buy ? "var(--good)" : "var(--bad)" }}>
        {it.label}
      </span>
      <span className="num whitespace-nowrap" style={{ color: "var(--text)" }}>{it.requested}</span>
      <span className="num whitespace-nowrap" style={{ color: filled ? "var(--text)" : "var(--muted)" }}>
        {filled ? it.filled : (UNFILLED[it.status ?? ""] ?? it.status)}
      </span>
      <span className="num text-right font-semibold" style={{ color: "var(--text)" }}>{it.remaining ?? ""}</span>
      <span />
    </>
  );
}
