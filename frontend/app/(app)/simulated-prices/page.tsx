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
 *
 * So the default is a DRY RUN: the server walks a scratch copy of the ladder
 * down the path with the same trim/stop/trail code and narrates each step.
 * Nothing is pinned, sent or saved, which also makes it the only mode that
 * shows anything while the market is closed.
 */

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
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
  avg_entry_price: string | null;
  pinned_price: string | null;
  entry_price: string | null;
  stop_price: string | null;
  trail_qty: string | null;
  trail_amount: string | null;
  peak_price: string | null;
  rung: number;
  ladder_history: LadderEvent[];
};

type LadderEvent = {
  id: string;
  rung: number;
  quantity: string;
  filled_quantity: string;
  quantity_before: string;
  quantity_remaining: string;
  stop_quantity: string;
  fill_price: string | null;
  status: string;
  note: string | null;
  // Null for a rung that only moved the stop — nothing records when it ran.
  happened_at: string | null;
};

type DryRunEvent = {
  kind: string; // hold | trim | trail_armed | stop_hit | trail_hit | flat | note
  text: string;
  rung: number | null;
  sold: string;
};

type DryRunStep = {
  index: number;
  pct: string;
  price: string;
  gain_pct: string | null;
  held: string;
  stop: string | null;
  events: DryRunEvent[];
};

type DryRun = {
  quantity: string;
  auto_trim_on: boolean;
  steps: DryRunStep[];
};

type SimLog = DryRun & { buy: string; shown: number };

const EVENT_TONE: Record<string, string> = {
  trim: "var(--good)",
  trail_hit: "var(--good)",
  trail_armed: "var(--warn)",
  stop_hit: "var(--bad)",
  note: "var(--warn)",
  hold: "var(--muted)",
  flat: "var(--muted)",
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
  danger: "var(--bad)",
  warn: "var(--warn)",
  ok: "var(--good)",
  idle: "var(--muted)",
};

const DEFAULT_PRICE_PATH = "0,10,25,35,50,60,75,85";

function formatPrice(value: number): string {
  return value.toFixed(4).replace(/\.?(0+)$/, "");
}

function ladderTime(value: string | null): string {
  return value
    ? new Date(value).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : "time not recorded";
}

export default function SimulatedPricesPage() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [disabled, setDisabled] = useState(false);
  // BUY PRICE is a local test input, seeded from the ladder's own entry price —
  // the number auto-trim measures its gates against.
  // SIMULATED PRICE is updated as each path step reaches the enforcement pipeline.
  const [buyPrices, setBuyPrices] = useState<Record<string, string>>({});
  const [marketPrices, setMarketPrices] = useState<Record<string, string>>({});
  const [pricePaths, setPricePaths] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [runningPaths, setRunningPaths] = useState<Record<string, boolean>>({});
  // View-only: clearing history never changes orders, stops, or positions.
  // Hidden by id, not by time — the server's ids are stable across refreshes,
  // and a cutoff would compare the browser's clock against the server's.
  const [hiddenHistory, setHiddenHistory] = useState<Set<string>>(() => new Set());
  const [autoRefresh, setAutoRefresh] = useState(true);
  // "dry": narrate the ladder server-side, place nothing. "live": pin each step
  // and let the real enforcement path act on it.
  const [mode, setMode] = useState<"dry" | "live">("dry");
  const [simLogs, setSimLogs] = useState<Record<string, SimLog>>({});
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const pathTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  // Each run gets a token; stopping or starting again bumps it, so a step whose
  // pin comes back after that sees a stale token and schedules nothing. Every
  // step can place real orders, so a stopped path must stay stopped.
  const pathRuns = useRef<Record<string, number>>({});
  // The step pin currently on the wire, so a clear can wait for it — otherwise
  // the server may apply that pin after the clear and quietly reinstate it.
  const pathInflight = useRef<Record<string, Promise<boolean>>>({});
  const mounted = useRef(true);

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

  async function pin(key: string, price: string, quiet = false): Promise<boolean> {
    setBusy(key);
    try {
      setRows(
        await api<Row[]>("/api/discord-sources/simulated-prices", {
          method: "POST",
          body: JSON.stringify({ key, price }),
        })
      );
      setMarketPrices((d) => {
        const next = { ...d };
        delete next[key];
        return next;
      });
      if (!quiet) notify.success(price ? `Pinned at ${price}` : "Back to the broker price");
      return true;
    } catch (e) {
      notify.fromError(e, "Could not set that price");
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function clearAll() {
    Object.keys(pathRuns.current).forEach(stopPricePath);
    await Promise.all(Object.values(pathInflight.current));
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

  function clearHistory() {
    setHiddenHistory((hidden) => {
      const next = new Set(hidden);
      (rows ?? []).forEach((row) => (row.ladder_history ?? []).forEach((e) => next.add(e.id)));
      return next;
    });
    notify.success("Ladder history cleared from this view");
  }

  function buyPriceFor(r: Row): string {
    // The ladder measures from entry_price, so a path in "% from buy" only
    // lines up with the trim gates when BUY PRICE is that same number. The
    // broker's average differs after averaging in, or a fill off the limit.
    return buyPrices[r.key] ?? r.entry_price ?? r.avg_entry_price ?? r.broker_price ?? "";
  }

  function marketPriceFor(r: Row): string {
    return marketPrices[r.key] ?? r.pinned_price ?? r.broker_price ?? "";
  }

  function parsePricePath(raw: string): number[] | null {
    const steps = raw.split(",").map((part) => Number(part.trim()));
    // Negative steps walk the price down into a stop; -100% would be a zero price.
    return steps.length > 0 && steps.every((pct) => Number.isFinite(pct) && pct > -100)
      ? steps
      : null;
  }

  function runPricePath(r: Row) {
    const buy = Number(buyPriceFor(r));
    if (!Number.isFinite(buy) || buy <= 0) {
      notify.error("Enter a positive BUY PRICE before running a path.");
      return;
    }
    const steps = parsePricePath(pricePaths[r.key] ?? DEFAULT_PRICE_PATH);
    if (!steps) {
      notify.error("PRICE PATH must be comma-separated percentages above -100.");
      return;
    }
    if (mode === "dry") {
      void runDryPath(r, buy, steps);
      return;
    }

    clearTimeout(pathTimers.current[r.key]);
    const token = (pathRuns.current[r.key] ?? 0) + 1;
    pathRuns.current[r.key] = token;
    const live = () => mounted.current && pathRuns.current[r.key] === token;
    setRunningPaths((paths) => ({ ...paths, [r.key]: true }));
    let index = 0;

    const step = async () => {
      if (!live()) return;
      const pct = steps[index];
      const price = formatPrice(buy * (1 + pct / 100));
      setMarketPrices((prices) => ({ ...prices, [r.key]: price }));
      const request = pin(r.key, price, true);
      pathInflight.current[r.key] = request;
      const pinned = await request;
      if (!live()) return;
      if (!pinned || index >= steps.length - 1) {
        setRunningPaths((paths) => ({ ...paths, [r.key]: false }));
        return;
      }
      index += 1;
      pathTimers.current[r.key] = setTimeout(() => void step(), 1000);
    };
    void step();
  }

  async function runDryPath(r: Row, buy: number, steps: number[]) {
    clearTimeout(pathTimers.current[r.key]);
    const token = (pathRuns.current[r.key] ?? 0) + 1;
    pathRuns.current[r.key] = token;
    const live = () => mounted.current && pathRuns.current[r.key] === token;
    setRunningPaths((paths) => ({ ...paths, [r.key]: true }));

    let run: DryRun;
    try {
      // The whole path is judged in one call; the page then plays it back a
      // step a second so it reads as it would unfold.
      run = await api<DryRun>("/api/discord-sources/simulated-prices/dry-run", {
        method: "POST",
        body: JSON.stringify({ key: r.key, buy_price: String(buy), path: steps.map(String) }),
      });
    } catch (e) {
      notify.fromError(e, "Could not run the dry run");
      if (live()) setRunningPaths((paths) => ({ ...paths, [r.key]: false }));
      return;
    }
    if (!live()) return;
    setSimLogs((logs) => ({ ...logs, [r.key]: { ...run, buy: formatPrice(buy), shown: 0 } }));

    let shown = 0;
    const reveal = () => {
      if (!live()) return;
      shown += 1;
      const step = run.steps[shown - 1];
      setSimLogs((logs) => (logs[r.key] ? { ...logs, [r.key]: { ...logs[r.key], shown } } : logs));
      setMarketPrices((prices) => ({ ...prices, [r.key]: step.price }));
      if (shown >= run.steps.length) {
        setRunningPaths((paths) => ({ ...paths, [r.key]: false }));
        return;
      }
      pathTimers.current[r.key] = setTimeout(reveal, 1000);
    };
    reveal();
  }

  function clearSimLog(key: string) {
    stopPricePath(key);
    setSimLogs((logs) => {
      const next = { ...logs };
      delete next[key];
      return next;
    });
    setMarketPrices((prices) => {
      const next = { ...prices };
      delete next[key];
      return next;
    });
  }

  function stopPricePath(key: string) {
    pathRuns.current[key] = (pathRuns.current[key] ?? 0) + 1;
    clearTimeout(pathTimers.current[key]);
    setRunningPaths((paths) => ({ ...paths, [key]: false }));
  }

  async function clearPin(key: string) {
    stopPricePath(key);
    await pathInflight.current[key];
    await pin(key, "");
  }

  useEffect(() => {
    mounted.current = true;
    // Same object for the page's lifetime (keys are mutated, never reassigned).
    const timers = pathTimers.current;
    return () => {
      mounted.current = false;
      Object.values(timers).forEach(clearTimeout);
    };
  }, []);

  const pinned = (rows ?? []).filter((r) => r.pinned_price);

  return (
    <div className="px-6 py-6 max-w-[1100px]">
      <div className="flex items-baseline justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-semibold" style={{ color: "var(--text)" }}>
            Simulated prices
          </h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            Run a price path to exercise the Discord exit ladder without waiting
            for the market to move.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <div
            role="radiogroup"
            aria-label="Run mode"
            className="flex rounded-lg overflow-hidden text-[12px]"
            style={{ border: "1px solid var(--border)" }}
          >
            {([
              ["dry", "Dry run · no orders"],
              ["live", "Live · real orders"],
            ] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={mode === value}
                disabled={Object.values(runningPaths).some(Boolean)}
                onClick={() => setMode(value)}
                className="px-3 py-1 disabled:opacity-40"
                style={{
                  background:
                    mode === value
                      ? value === "live" ? "var(--bad)" : "var(--accent)"
                      : "transparent",
                  color: mode === value ? "#fff" : "var(--muted)",
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={clearHistory}
            disabled={!rows?.some((row) =>
              (row.ladder_history ?? []).some((e) => !hiddenHistory.has(e.id))
            )}
            className="btn-ghost px-3 py-1 text-[12px] disabled:opacity-40"
          >
            Clear history
          </button>
          <label className="flex items-center gap-2 text-[12px]" style={{ color: "var(--muted)" }}>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            Refresh every 4s
          </label>
        </div>
      </div>

      {mode === "dry" ? (
        <div
          className="mt-4 rounded-xl px-4 py-3 text-[12.5px] leading-relaxed"
          style={{ border: "1px solid var(--border)", background: "var(--panel)", color: "var(--text)" }}
        >
          <strong>Dry run — nothing is sent to your broker.</strong> Each path runs
          your current ladder settings through the same trim, stop and trailing
          code as live trading, starting from a fresh entry at BUY PRICE, and
          narrates every step below the row. Fills are assumed in full at the
          step&rsquo;s price; a real order fills at the market price, and a broker
          can refuse a stop that the dry run accepts. Works with the market closed.
        </div>
      ) : (
        <div
          className="mt-4 rounded-xl px-4 py-3 text-[12.5px] leading-relaxed"
          style={{
            background: "var(--bad-soft)",
            border: "1px solid var(--bad)",
            color: "var(--text)",
          }}
        >
          <strong>A simulated price can place real orders.</strong> Each path step
          feeds the same enforcement path as a real quote, so a price below a stop
          can submit an order — and your broker fills it at the <em>real</em> price,
          not the simulated one. That is what makes this a useful test and what
          makes it worth being careful with. Simulated prices expire after an hour.
          Needs a live ladder on the position and an open market. While a contract
          is pinned its stop is checked here rather than resting at Alpaca (which
          would judge it against the real price) — but the trims and stop levels a
          pin causes are real, and are judged against the real price once the pin
          clears.
        </div>
      )}

      {disabled && (
        <p className="mt-6 text-sm" style={{ color: "var(--muted)" }}>
          Price pinning is switched off in this environment. Set{" "}
          <code>DISCORD_PRICE_OVERRIDE_ENABLED=true</code> and restart the backend.
        </p>
      )}

      {pinned.length > 0 && (
        <div className="mt-4 flex items-center gap-3">
          <span className="text-[12px]" style={{ color: "var(--warn)" }}>
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
          <table className="w-full text-sm" style={{ minWidth: 620 }}>
            <thead>
              <tr style={{ background: "var(--panel-2)" }}>
                {["Contract", "Qty", "BUY PRICE", "Broker", "SIMULATED PRICE", "PRICE PATH (% FROM BUY)", "", "Ladder"].map(
                  (h) => (
                    <th
                      key={h}
                      className="text-left font-medium text-[10px] uppercase tracking-wide px-0.5 py-1.5"
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
                const marketPrice = marketPriceFor(r);
                const history = (r.ladder_history ?? []).filter(
                  (event) => !hiddenHistory.has(event.id)
                );
                const log = simLogs[r.key];
                return (
                  <Fragment key={r.key}>
                  <tr style={{ borderTop: "1px solid var(--border)" }}>
                    <td className="pl-2 pr-0 py-1.5" style={{ color: "var(--text)" }}>
                      {contractLabel(r)}
                      {r.rung > 0 && (
                        <span
                          className="ml-1 text-[10px]"
                          title={`Trim ${r.rung} complete; the next configured trim is now eligible.`}
                          style={{ color: "var(--muted)" }}
                        >
                          trim {r.rung} complete
                        </span>
                      )}
                    </td>
                    <td className="px-0 py-1.5 tabular-nums" style={{ color: "var(--text-2)" }}>
                      {r.quantity}
                    </td>
                    <td className="px-1 py-1.5">
                      <input
                        type="number"
                        step="0.0001"
                        min="0"
                        aria-label={`Buy price for ${contractLabel(r)}`}
                        title={
                          r.entry_price
                            ? `Ladder entry ${r.entry_price}` +
                              (r.avg_entry_price ? ` · broker average ${r.avg_entry_price}` : "")
                            : undefined
                        }
                        value={buyPriceFor(r)}
                        disabled={busy === r.key}
                        onChange={(e) => setBuyPrices((p) => ({ ...p, [r.key]: e.target.value }))}
                        className="rounded-lg border px-2 py-1 text-sm bg-transparent focus-ring tabular-nums"
                        style={{ borderColor: "var(--border)", color: "var(--text)", width: 72 }}
                      />
                    </td>
                    <td className="px-1 py-1.5 tabular-nums" style={{ color: "var(--muted)" }}>
                      {r.broker_price ?? "—"}
                    </td>
                    <td className="pl-1 pr-0 py-1.5">
                      <span className="tabular-nums" style={{ color: r.pinned_price ? "var(--warn)" : "var(--text)" }}>
                        {marketPrice || "—"}
                      </span>
                      {r.pinned_price ? (
                        <span className="ml-1 text-[10px] uppercase tracking-wide" style={{ color: "var(--warn)" }}>pinned</span>
                      ) : log && marketPrices[r.key] ? (
                        <span className="ml-1 text-[10px] uppercase tracking-wide" style={{ color: "var(--accent)" }}>dry run</span>
                      ) : null}
                    </td>
                    <td className="px-0 py-1.5">
                      <input
                        type="text"
                        aria-label={`Price path percentage from buy for ${contractLabel(r)}`}
                        value={pricePaths[r.key] ?? DEFAULT_PRICE_PATH}
                        disabled={busy === r.key || runningPaths[r.key]}
                        onChange={(e) => setPricePaths((paths) => ({ ...paths, [r.key]: e.target.value }))}
                        className="rounded-lg border px-2 py-1 text-sm bg-transparent focus-ring tabular-nums"
                        style={{ borderColor: "var(--border)", color: "var(--text)", width: 142 }}
                      />
                    </td>
                    <td className="pl-0 pr-1 py-1.5 align-top whitespace-nowrap">
                      <div className="flex items-center gap-1.5 justify-end">
                        {runningPaths[r.key] ? (
                          <button
                            type="button"
                            onClick={() => stopPricePath(r.key)}
                            className="btn-danger-soft px-2.5 py-1 text-[12px]"
                          >
                            Stop
                          </button>
                        ) : (
                          <button
                            type="button"
                            disabled={busy === r.key}
                            onClick={() => runPricePath(r)}
                            className="btn-primary px-2.5 py-1 text-[12px] disabled:opacity-40"
                          >
                            Run path
                          </button>
                        )}
                        {r.pinned_price && (
                          <button
                            type="button"
                            disabled={busy === r.key}
                            onClick={() => void clearPin(r.key)}
                            title="Stop any running path and return this contract to the broker price"
                            className="btn-ghost px-2 py-1 text-[12px] disabled:opacity-40"
                          >
                            Clear
                          </button>
                        )}
                      </div>
                    </td>
                    <td className="pl-2 pr-2 py-1.5 text-[11px]" style={{ color: TONE[v.tone], minWidth: 280 }}>
                      <div>{v.text}</div>
                      {history.length > 0 && (
                        <div className="mt-1 space-y-0.5" style={{ color: "var(--text-2)" }}>
                          {history.map((event) => (
                            <div key={event.id} className="tabular-nums">
                              {event.status === "stop_only" ? (
                                <>T{event.rung} · {event.note} · stop {event.stop_quantity} · {ladderTime(event.happened_at)}</>
                              ) : event.status === "armed" ? (
                                <>T{event.rung} · stop {event.stop_quantity} · {event.note} · {ladderTime(event.happened_at)}</>
                              ) : (
                                <>
                                  T{event.rung} · Qty: {event.quantity} · sold {event.filled_quantity}
                                  {` · stop ${event.stop_quantity}`}
                                  {event.fill_price ? ` @ ${event.fill_price}` : ""}
                                  {` · ${event.status.toLowerCase()} · ${ladderTime(event.happened_at)}`}
                                </>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </td>
                  </tr>
                  {log && (
                    <tr>
                      <td colSpan={8} className="px-3 pb-3 pt-0">
                        <div
                          className="rounded-lg px-3 py-2 text-[12px]"
                          style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}
                        >
                          <div className="flex items-center justify-between gap-3 mb-1.5">
                            <span style={{ color: "var(--text-2)" }}>
                              <strong style={{ color: "var(--text)" }}>Dry run</strong> · {log.quantity}{" "}
                              contract{log.quantity === "1" ? "" : "s"} bought at {log.buy}
                              {!log.auto_trim_on && (
                                <span style={{ color: "var(--warn)" }}>
                                  {" "}· Auto Trim is off in your Discord settings — simulated as if on
                                </span>
                              )}
                            </span>
                            <button
                              type="button"
                              onClick={() => clearSimLog(r.key)}
                              className="btn-ghost px-2 py-0.5 text-[11px]"
                            >
                              Clear
                            </button>
                          </div>
                          <ol className="space-y-1">
                            {log.steps.slice(0, log.shown).map((step) => (
                              <li key={step.index} className="flex gap-3">
                                <span className="tabular-nums shrink-0" style={{ color: "var(--muted)", width: 156 }}>
                                  Step {step.index} · {step.price}{" "}
                                  ({Number(step.pct) >= 0 ? "+" : ""}{step.pct}%)
                                </span>
                                <span className="space-y-0.5">
                                  {step.events.map((event, i) => (
                                    <span key={i} className="block" style={{ color: EVENT_TONE[event.kind] ?? "var(--text)" }}>
                                      {event.text}
                                    </span>
                                  ))}
                                </span>
                              </li>
                            ))}
                          </ol>
                          {log.shown >= log.steps.length && (
                            <div className="mt-1.5 pt-1.5 tabular-nums" style={{ borderTop: "1px solid var(--border)", color: "var(--text-2)" }}>
                              End of path · {log.steps[log.steps.length - 1]?.held ?? "0"} held
                              {log.steps[log.steps.length - 1]?.stop ? ` · stop ${log.steps[log.steps.length - 1].stop}` : ""}
                            </div>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
