"use client";

// Live price store (phase 4). The SSE stream (lib/sse.ts) feeds price.tick
// events here; components read a symbol's live price via useLivePrice(), which
// re-renders ONLY when that symbol ticks. Falls back to the passed value until
// a tick arrives, so nothing goes blank before the stream warms up.
import { useEffect, useMemo, useState } from "react";

const _store = new Map<string, number>();
const _subs = new Map<string, Set<(p: number) => void>>();

/** Read a symbol's live price without subscribing. For aggregate consumers that
 *  subscribe to a whole set via useLivePrices() and then read each value. */
export function peekLivePrice(symbol: string | null | undefined): number | null {
  const sym = symbol ? String(symbol).toUpperCase() : "";
  return sym ? _store.get(sym) ?? null : null;
}

/** Subscribe to a SET of symbols; returns a counter that bumps only when one of
 *  THEM ticks (not every symbol platform-wide). Consumers read current values
 *  with peekLivePrice(). Re-subscribes when the set changes. Keeps aggregate
 *  re-renders scoped to the component that calls it. */
export function useLivePrices(symbols: string[]): number {
  const key = useMemo(() => Array.from(new Set(symbols.map((s) => s.toUpperCase()))).sort().join(","), [symbols]);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (!key) return;
    const bump = () => setVersion((v) => v + 1);
    const syms = key.split(",");
    for (const sym of syms) {
      let set = _subs.get(sym);
      if (!set) { set = new Set(); _subs.set(sym, set); }
      set.add(bump);
    }
    return () => {
      for (const sym of syms) {
        const set = _subs.get(sym);
        if (set) { set.delete(bump); if (set.size === 0) _subs.delete(sym); }
      }
    };
  }, [key]);
  return version;
}

export function applyPriceTick(evt: { symbol?: string; price?: string | number } | null): void {
  const sym = evt?.symbol ? String(evt.symbol).toUpperCase() : "";
  const p = evt?.price == null ? NaN : Number(evt.price);
  if (!sym || !Number.isFinite(p) || p <= 0) return;
  _store.set(sym, p);
  const set = _subs.get(sym);
  if (set) set.forEach((fn) => { try { fn(p); } catch { /* ignore */ } });
}

/** Live price for `symbol`, or `fallback` until a tick arrives. Re-renders only
 *  when THIS symbol ticks — not on every symbol's tick. */
export function useLivePrice(
  symbol: string | null | undefined,
  fallback: number | string | null | undefined,
): number | null {
  const sym = symbol ? String(symbol).toUpperCase() : "";
  const [live, setLive] = useState<number | null>(() => (sym ? _store.get(sym) ?? null : null));

  useEffect(() => {
    setLive(sym ? _store.get(sym) ?? null : null);
    if (!sym) return;
    let set = _subs.get(sym);
    if (!set) { set = new Set(); _subs.set(sym, set); }
    const fn = (p: number) => setLive(p);
    set.add(fn);
    return () => {
      set!.delete(fn);
      if (set!.size === 0) _subs.delete(sym);
    };
  }, [sym]);

  if (live != null) return live;
  const fb = fallback == null ? null : Number(fallback);
  return fb != null && Number.isFinite(fb) ? fb : null;
}
