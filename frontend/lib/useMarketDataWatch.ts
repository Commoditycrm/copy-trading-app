"use client";

import { useEffect } from "react";
import { api } from "@/lib/api";
import { seedPrice } from "@/lib/livePrices";

/**
 * Register `symbols` as watched on the central market-data stream, heartbeated
 * every 30s — but ONLY while the tab is visible. When the tab is hidden
 * (backgrounded or switched away) the heartbeat pauses, so the watch TTL lapses
 * and the stream + SSE stop spending on symbols nobody is looking at. Resumes
 * immediately when the tab becomes visible again.
 *
 * Replaces the per-page ad-hoc watch effects (positions/trade-panel/order-
 * history/snapshot) with one implementation that also does the visibility gate.
 */
export function useMarketDataWatch(symbols: string[]): void {
  // Stable primitive dep so a new array identity each render doesn't re-run.
  const key = Array.from(new Set(symbols.filter(Boolean).map((s) => s.toUpperCase()))).sort().join(",");

  useEffect(() => {
    if (!key) return;
    const syms = key.split(",");
    let timer: ReturnType<typeof setInterval> | null = null;

    const ping = () => {
      // Guard again at fire time — the tab may have hidden between ticks.
      if (typeof document !== "undefined" && document.hidden) return;
      // The response carries a current price per symbol (cache, else a one-shot
      // REST quote). Seed the live store with it: without this a cell showed
      // "—" until the stream ticked, which for a quiet option contract (a
      // Discord alert awaiting approval in Order History) could be never.
      api<{ prices?: Record<string, string | null> }>("/api/market-data/watch", {
        method: "POST",
        body: JSON.stringify({ symbols: syms }),
      })
        .then((r) => {
          for (const [sym, px] of Object.entries(r?.prices ?? {})) seedPrice(sym, px);
        })
        .catch(() => {});
    };
    const start = () => {
      if (timer) return;
      ping();
      timer = setInterval(ping, 30_000);
    };
    const stop = () => {
      if (timer) { clearInterval(timer); timer = null; }
    };
    const onVisibility = () => {
      if (typeof document !== "undefined" && document.hidden) stop();
      else start();
    };

    if (typeof document === "undefined" || !document.hidden) start();
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibility);
    }
    return () => {
      stop();
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibility);
      }
    };
  }, [key]);
}
