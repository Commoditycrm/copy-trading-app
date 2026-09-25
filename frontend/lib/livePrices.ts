"use client";

// Live price store (phase 4). The SSE stream (lib/sse.ts) feeds price.tick
// events here; components read a symbol's live price via useLivePrice(), which
// re-renders ONLY when that symbol ticks. Falls back to the passed value until
// a tick arrives, so nothing goes blank before the stream warms up.
import { useEffect, useState } from "react";

const _store = new Map<string, number>();
const _subs = new Map<string, Set<(p: number) => void>>();

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
