"use client";

/**
 * Simulated prices — a testing screen.
 *
 * The Discord trim ladder only moves when the price does, so proving its stops
 * and trailing exits against a quiet market means waiting for a move that may
 * never come. Pinning a price here lets the real enforcement path run against a
 * number you choose.
 *
 * It is deliberately blunt about what that means: a pinned price feeds the SAME
 * code that places orders. With live trading on, a pin below a stop places a
 * REAL order — filled at the REAL price, not the pinned one.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

type Row = {
  key: string;
  symbol: string;
  option_strike: string | null;
  option_right: string | null;
  option_expiry: string | null;
  quantity: string;
  broker_price: string | null;
  pinned_price: string | null;
  entry_price: string | null;
  stop_price: string | null;
  trail_qty: string | null;
  trail_amount: string | null;
  peak_price: string | null;
  rung: number;
};

function contractLabel(r: Row) {
  if (!r.option_strike) return r.symbol;
  const right = r.option_right === "put" ? "P" : "C";
  const when = r.option_expiry
    ? new Date(r.option_expiry + "T00:00:00").toLocaleDateString(undefined, {
        day: "numeric",
        month: "short",
      })
    : "";
  return `${r.symbol} ${right} $${r.option_strike} ${when}`.trim();
}

/** What the ladder will do to this position at the price it is being judged at. */
function verdict(r: Row): { text: string; tone: "danger" | "warn" | "ok" | "idle" } {
  const price = Number(r.pinned_price ?? r.broker_price ?? NaN);
  if (!Number.isFinite(price)) return { text: "no price", tone: "idle" };

  if (r.stop_price && price <= Number(r.stop_price)) {
    return { text: `stop breached — closes ${r.quantity}`, tone: "danger" };
  }
  if (r.trail_qty && r.trail_amount && r.peak_price) {
    const trigger = Number(r.peak_price) - Number(r.trail_amount);
    if (price <= trigger) {
      return { text: `trail hit — sells ${r.trail_qty}`, tone: "danger" };
    }
    return {
      text: `trailing ${r.trail_qty} · fires at ${trigger.toFixed(2)}`,
      tone: "warn",
    };
  }
  if (r.stop_price) {
    return { text: `holding · stop ${r.stop_price}`, tone: "ok" };
  }
  return { text: "nothing armed", tone: "idle" };
}

const TONE: Record<string, string> = {
  danger: "var(--danger)",
  warn: "var(--warning)",
  ok: "var(--success)",
  idle: "var(--muted)",
};

export default function SimulatedPricesPage() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [disabled, setDisabled] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      setRows(await api<Row[]>("/api/discord-sources/simulated-prices"));
      setDisabled(false);
    } catch (e) {
      const msg = String(e);
      if (msg.includes("price_override_disabled") || msg.includes("503")) {
        setDisabled(true);
        setRows([]);
      } else {
        notify.fromError(e, "Could not load positions");
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // The enforcer runs on the poller's clock, not ours — so the effect of a pin
  // shows up a tick later. Polling is what makes that visible.
  useEffect(() => {
    if (timer.current) clearInterval(timer.current);
    if (autoRefresh) timer.current = setInterval(() => void load(), 4000);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [autoRefresh, load]);

  async function pin(key: string, price: string) {
    setBusy(key);
    try {
      setRows(
        await api<Row[]>("/api/discord-sources/simulated-prices", {
          method: "POST",
          body: JSON.stringify({ key, price }),
        })
      );
      setDrafts((d) => ({ ...d, [key]: "" }));
      notify.success(price ? `Pinned at ${price}` : "Back to the broker price");
    } catch (e) {
      notify.fromError(e, "Could not set that price");
    } finally {
      setBusy(null);
    }
  }

  async function clearAll() {
    setBusy("__all__");
    try {
      await api("/api/discord-sources/simulated-prices", { method: "DELETE" });
      await load();
      notify.success("All prices back to the broker");
    } catch (e) {
      notify.fromError(e, "Could not clear pins");
    } finally {
      setBusy(null);
    }
  }

  const pinned = (rows ?? []).filter((r) => r.pinned_price);

  return (
    <div className="px-6 py-6 max-w-[1100px]">
      <div className="flex items-baseline justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-semibold" style={{ color: "var(--text)" }}>
            Simulated prices
          </h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            Pin a contract&rsquo;s price to exercise the Discord exit ladder without
            waiting for the market to move.
          </p>
        </div>
        <label className="flex items-center gap-2 text-[12px]" style={{ color: "var(--muted)" }}>
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
          />
          Refresh every 4s
        </label>
      </div>

      <div
        className="mt-4 rounded-xl px-4 py-3 text-[12.5px] leading-relaxed"
        style={{
          background: "var(--danger-soft, rgba(220,60,50,.08))",
          border: "1px solid var(--danger)",
          color: "var(--text)",
        }}
      >
        <strong>A pinned price places real orders.</strong> It feeds the same
        enforcement path as a real quote, so a pin below a stop will submit an
        order — and your broker fills it at the <em>real</em> price, not the pinned
        one. That is what makes this a useful test and what makes it worth being
        careful with. Pins expire after an hour.
      </div>

      {disabled && (
        <p className="mt-6 text-sm" style={{ color: "var(--muted)" }}>
          Price pinning is switched off in this environment. Set{" "}
          <code>DISCORD_PRICE_OVERRIDE_ENABLED=true</code> and restart the backend.
        </p>
      )}

      {pinned.length > 0 && (
        <div className="mt-4 flex items-center gap-3">
          <span className="text-[12px]" style={{ color: "var(--warning)" }}>
            {pinned.length} position{pinned.length > 1 ? "s" : ""} on a pinned price
          </span>
          <button
            type="button"
            onClick={clearAll}
            disabled={busy === "__all__"}
            className="btn-ghost px-3 py-1 text-[12px]"
          >
            Back to broker prices
          </button>
        </div>
      )}

      {rows === null && (
        <p className="mt-6 text-sm" style={{ color: "var(--muted)" }}>
          Loading positions&hellip;
        </p>
      )}
      {rows !== null && rows.length === 0 && !disabled && (
        <p className="mt-6 text-sm" style={{ color: "var(--muted)" }}>
          No open positions to simulate against.
        </p>
      )}

      {rows !== null && rows.length > 0 && (
        <div
          className="mt-5 rounded-xl overflow-x-auto"
          style={{ border: "1px solid var(--border)", background: "var(--panel)" }}
        >
          <table className="w-full text-sm" style={{ minWidth: 860 }}>
            <thead>
              <tr style={{ background: "var(--panel-2)" }}>
                {["Contract", "Qty", "Entry", "Broker", "Price used", "Ladder", ""].map(
                  (h) => (
                    <th
                      key={h}
                      className="text-left font-medium text-[11px] uppercase tracking-wide px-4 py-2.5"
                      style={{ color: "var(--text-2)", borderBottom: "1px solid var(--border)" }}
                    >
                      {h}
                    </th>
                  )
                )}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const v = verdict(r);
                const live = r.pinned_price ?? r.broker_price ?? "—";
                return (
                  <tr key={r.key} style={{ borderTop: "1px solid var(--border)" }}>
                    <td className="px-4 py-3" style={{ color: "var(--text)" }}>
                      {contractLabel(r)}
                      {r.rung > 0 && (
                        <span className="ml-2 text-[10px]" style={{ color: "var(--muted)" }}>
                          rung {r.rung}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 tabular-nums" style={{ color: "var(--text-2)" }}>
                      {r.quantity}
                    </td>
                    <td className="px-4 py-3 tabular-nums" style={{ color: "var(--text-2)" }}>
                      {r.entry_price ?? "—"}
                    </td>
                    <td className="px-4 py-3 tabular-nums" style={{ color: "var(--muted)" }}>
                      {r.broker_price ?? "—"}
                    </td>
                    <td className="px-4 py-3 tabular-nums font-medium"
                        style={{ color: r.pinned_price ? "var(--warning)" : "var(--text)" }}>
                      {live}
                      {r.pinned_price && (
                        <span className="ml-1.5 text-[10px] uppercase tracking-wide">pinned</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-[12px]" style={{ color: TONE[v.tone] }}>
                      {v.text}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-1.5 justify-end">
                        <input
                          type="number"
                          step="0.01"
                          min="0"
                          placeholder={r.broker_price ?? "price"}
                          value={drafts[r.key] ?? ""}
                          disabled={busy === r.key}
                          onChange={(e) =>
                            setDrafts((d) => ({ ...d, [r.key]: e.target.value }))
                          }
                          onKeyDown={(e) => {
                            if (e.key === "Enter" && drafts[r.key])
                              void pin(r.key, drafts[r.key]);
                          }}
                          className="rounded-lg border px-2 py-1 text-sm bg-transparent focus-ring tabular-nums"
                          style={{ borderColor: "var(--border)", color: "var(--text)", width: 92 }}
                        />
                        <button
                          type="button"
                          disabled={busy === r.key || !drafts[r.key]}
                          onClick={() => void pin(r.key, drafts[r.key])}
                          className="btn-primary px-2.5 py-1 text-[12px] disabled:opacity-40"
                        >
                          Pin
                        </button>
                        {r.pinned_price && (
                          <button
                            type="button"
                            disabled={busy === r.key}
                            onClick={() => void pin(r.key, "")}
                            className="btn-ghost px-2 py-1 text-[12px]"
                          >
                            Clear
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
