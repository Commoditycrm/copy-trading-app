"use client";

/**
 * Position summary: the rules that govern a holding, what is placed on it, and
 * everything that happened to it, oldest first —
 *
 *     Entry Settings: Mark · market · 4× the alert's size · live
 *     Exit Settings:  take-profit orders · T1: +20%, sell 50%, stop -25% · …
 *     Placed:         TP 0.50 · T1 · 4 · Webull  ×
 *
 *     11:58:04  Entry    4 @ market      4 @ 0.41   4   0.41    Mark alert: "…"
 *     11:58:50  Average  4 @ 0.38 limit  4 @ 0.38   8   0.395   averaged by you
 *
 * Orders show what was asked for and what filled; the stop's own history sits
 * between them in time; "Why" says what caused each line. Shown in the panel
 * under an open position (with ``placed``), and under a sold one in Closed
 * today (``throughOrderId`` = the sell, for the holding it closed). Reads
 * GET /api/positions/history — our own records, no broker call — and reads it
 * again whenever an order event arrives, so a trim or sell shows at once.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { fmtDateTimeMs, fmtSignedUsd } from "@/lib/format";
import { useEventStream } from "@/lib/sse";

type Item = {
  type: "order" | "event";
  at: string | null;
  label: string;
  side: "buy" | "sell" | null;
  requested: string | null;
  filled: string | null;
  status: string | null;
  remaining: string | null;
  /** Average cost of what is still held after this fill (null when flat). */
  avg_price: string | null;
  /** Realized P&L of a sell, in dollars (signed); null for buys and events. */
  pnl: string | null;
  /** Why it happened: the alert, auto-trim, a stop, you on Positions … */
  note: string | null;
  detail: string | null;
};

type Rules = {
  channel: string;
  quantity: string;
  max_per_contract: string | null;
  max_per_order: string | null;
  exits: string;
  entries: string;
  mode: string;
  on_fill: string | null;
  ladder: { trim: number; target: string; sells: string; stop: string; state: string }[];
  entry_price: string | null;
  stop_now: string | null;
  trailing_now: string | null;
  closed: string | null;
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

/** Order events that can change a holding's history. */
const REFRESH_ON = new Set([
  "order.placed", "order.updated", "order.cancelled", "order.copy_submitted", "position.auto_closed",
]);
/** After an event: once quickly, once more to catch the fill the event announced. */
const REFRESH_AFTER_MS = [1_200, 6_000];

export function PositionSummary({ target, placed }: { target: SummaryTarget; placed?: React.ReactNode }) {
  const [items, setItems] = useState<Item[] | null>(null);
  const [rules, setRules] = useState<Rules | null>(null);
  const [error, setError] = useState(false);

  const q = new URLSearchParams({ broker_account_id: target.brokerAccountId, symbol: target.symbol });
  if (target.optionStrike) q.set("option_strike", target.optionStrike);
  if (target.optionRight) q.set("option_right", target.optionRight);
  if (target.optionExpiry) q.set("option_expiry", target.optionExpiry.slice(0, 10));
  if (target.throughOrderId) q.set("through_order_id", target.throughOrderId);
  const url = `/api/positions/history?${q.toString()}`;

  const mounted = useRef(true);
  const haveData = useRef(false);
  const load = useCallback(() => {
    api<{ rules: Rules | null; items: Item[] }>(url)
      .then((r) => {
        if (!mounted.current) return;
        haveData.current = true;
        setItems(r.items);
        setRules(r.rules);
        setError(false);
      })
      // A failed refresh keeps what is shown; only a first load that fails says so.
      .catch(() => { if (mounted.current && !haveData.current) setError(true); });
  }, [url]);

  useEffect(() => {
    mounted.current = true;
    load();
    return () => { mounted.current = false; };
  }, [load]);

  // A trim, a sell, a stop order: re-read as soon as the app hears of it.
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEventStream((evt) => {
    if (!REFRESH_ON.has(evt.type)) return;
    for (const t of timers.current) clearTimeout(t);
    timers.current = REFRESH_AFTER_MS.map((ms) => setTimeout(load, ms));
  });
  useEffect(() => () => { for (const t of timers.current) clearTimeout(t); }, []);

  const muted = { color: "var(--muted)" } as const;
  return (
    <div className="text-[12px]">
      {(rules || placed) && (
        <div className="mb-1.5 pb-1.5 leading-relaxed" style={{ borderBottom: "1px solid var(--border)" }}>
          {rules && <RulesLines rules={rules} />}
          {placed}
        </div>
      )}
      {error ? (
        <div style={muted}>Couldn&apos;t load this position&apos;s history.</div>
      ) : items === null ? (
        <div style={muted}>Loading…</div>
      ) : items.length === 0 ? (
        <div style={muted}>
          Nothing placed through Kopyya for this position — it may have been opened in the broker&apos;s own app.
        </div>
      ) : (
        <div className="grid gap-x-3 gap-y-0.5 items-center"
             style={{ gridTemplateColumns: "auto auto auto auto auto auto auto minmax(0,1fr)" }}>
          <span style={muted} title="Eastern time">Time</span>
          <span style={muted}>Event</span>
          <span style={muted}>Requested</span>
          <span style={muted}>Filled</span>
          <span className="text-right" style={muted}>Rem.Qty</span>
          <span className="text-right" style={muted} title="Average cost of what is still held">Avg.Price</span>
          <span className="text-right" style={muted} title="What each sell realized">P/L</span>
          <span style={muted}>Why</span>
          {items.map((it, i) => <Row key={i} it={it} />)}
        </div>
      )}
    </div>
  );
}

/** "11:58:04" today, "10/6 11:58" on an earlier day — ET either way. */
function shortTime(iso: string): string {
  const d = new Date(iso);
  const day = (x: Date) => x.toLocaleDateString("en-US", { timeZone: "America/New_York" });
  if (day(d) === day(new Date())) {
    return d.toLocaleTimeString("en-US", { timeZone: "America/New_York", hour12: false });
  }
  const md = d.toLocaleDateString("en-US", { timeZone: "America/New_York", month: "numeric", day: "numeric" });
  const hm = d.toLocaleTimeString("en-US", { timeZone: "America/New_York", hour12: false, hour: "2-digit", minute: "2-digit" });
  return `${md} ${hm}`;
}

/** How an order that has not filled reads in the Filled column. */
const UNFILLED: Record<string, string> = {
  pending: "waiting", submitted: "resting", accepted: "resting",
  partially_filled: "part filled", rejected: "rejected",
};

function Row({ it }: { it: Item }) {
  const time = (
    <span className="num whitespace-nowrap" style={{ color: "var(--muted)" }}
          title={it.at ? fmtDateTimeMs(it.at, "America/New_York") : undefined}>
      {it.at ? shortTime(it.at) : "—"}
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
        <span />
        <Why note={it.note} />
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
      <span className="num text-right" style={{ color: "var(--text-2)" }}>
        {filled ? (it.avg_price ?? "—") : ""}
      </span>
      <PnlCell value={it.pnl} />
      <Why note={it.note} />
    </>
  );
}

/** A sell's realized P&L: green for a gain, red for a loss, blank otherwise. */
function PnlCell({ value }: { value: string | null }) {
  const n = value == null ? null : Number(value);
  if (n == null || !Number.isFinite(n)) return <span />;
  return (
    <span className="num text-right font-semibold"
          style={{ color: n > 0 ? "var(--good)" : n < 0 ? "var(--bad)" : "var(--text-2)" }}>
      {fmtSignedUsd(n)}
    </span>
  );
}

/** Why a line happened, muted, in the last column. */
function Why({ note }: { note: string | null }) {
  return (
    <span className="text-[11px] leading-snug min-w-0 truncate" style={{ color: "var(--muted)" }} title={note ?? undefined}>
      {note ?? ""}
    </span>
  );
}

/** The settings that govern this position: one line for entries, one for
 *  exits (the whole ladder, T1 T2 T3 …, inline, then where it stands now). */
function RulesLines({ rules }: { rules: Rules }) {
  const muted = { color: "var(--muted)" } as const;
  const sep = <span style={muted}> · </span>;
  const entry: string[] = [
    rules.channel,
    rules.entries,
    rules.quantity,
    ...(rules.max_per_contract ? [`max ${rules.max_per_contract}/contract`] : []),
    ...(rules.max_per_order ? [`max ${rules.max_per_order}/order`] : []),
    rules.mode,
  ];
  const now: string[] = [
    ...(rules.entry_price ? [`entry ${rules.entry_price}`] : []),
    ...(rules.stop_now ? [`stop ${rules.stop_now}${rules.trailing_now ? ` (trailing ${rules.trailing_now})` : ""}`] : []),
    ...(rules.closed ? [`ladder finished: ${rules.closed}`] : []),
  ];
  return (
    <div style={{ color: "var(--text-2)" }}>
      <div>
        <span className="font-semibold" style={{ color: "var(--text)" }}>Entry Settings:</span>{" "}
        {entry.map((e, i) => <span key={i}>{i > 0 && sep}{e}</span>)}
      </div>
      <div>
        <span className="font-semibold" style={{ color: "var(--text)" }}>Exit Settings:</span>{" "}
        {rules.exits}
        {rules.on_fill && <>{sep}<span>On Fill: {rules.on_fill}</span></>}
        {rules.ladder.map((r) => {
          const done = r.state === "done";
          const next = r.state === "next";
          return (
            <span key={r.trim}>
              {sep}
              <span
                title={done ? "done" : next ? "next" : undefined}
                style={{
                  color: done ? "var(--muted)" : next ? "var(--accent)" : "var(--text-2)",
                  textDecoration: done ? "line-through" : undefined,
                  fontWeight: next ? 600 : undefined,
                }}
              >
                T{r.trim}: {r.target}, {r.sells}, {r.stop}
              </span>
            </span>
          );
        })}
        {now.length > 0 && <span style={muted}>{"  ·  now: "}{now.join(", ")}</span>}
      </div>
    </div>
  );
}
