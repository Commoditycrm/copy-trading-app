"use client";

// Live price store (phase 4). The SSE stream (lib/sse.ts) feeds price.tick
// events here; components read a symbol's live price via useLivePrice(), which
// re-renders ONLY when that symbol ticks. Falls back to the passed value until
// a tick arrives, so nothing goes blank before the stream warms up.
//
// Each tick carries the mid (`price`) and, when available, `bid`/`ask` — the
// trade panel reads the full quote via useLiveQuote(); positions read the mid.
import { useEffect, useMemo, useState } from "react";

export interface LiveQuote {
  mid: number;
  bid?: number;
  ask?: number;
}

const _store = new Map<string, LiveQuote>();
const _subs = new Map<string, Set<(q: LiveQuote) => void>>();

function _num(v: unknown): number | undefined {
  const n = v == null ? NaN : Number(v);
  return Number.isFinite(n) && n > 0 ? n : undefined;
}

export function applyPriceTick(
  evt: { symbol?: string; price?: string | number; bid?: string | number; ask?: string | number } | null,
): void {
  const sym = evt?.symbol ? String(evt.symbol).toUpperCase() : "";
  const mid = evt?.price == null ? NaN : Number(evt.price);
  if (!sym || !Number.isFinite(mid) || mid <= 0) return;
  const q: LiveQuote = { mid, bid: _num(evt?.bid), ask: _num(evt?.ask) };
  _store.set(sym, q);
  const set = _subs.get(sym);
  if (set) set.forEach((fn) => { try { fn(q); } catch { /* ignore */ } });
}

function _subscribe(sym: string, fn: (q: LiveQuote) => void): () => void {
  let set = _subs.get(sym);
  if (!set) { set = new Set(); _subs.set(sym, set); }
  set.add(fn);
  return () => {
    const s = _subs.get(sym);
    if (s) { s.delete(fn); if (s.size === 0) _subs.delete(sym); }
  };
}

/** Live mid for `symbol`, or `fallback` until a tick arrives. Re-renders only
 *  when THIS symbol ticks — not on every symbol's tick. */
export function useLivePrice(
  symbol: string | null | undefined,
  fallback: number | string | null | undefined,
): number | null {
  const sym = symbol ? String(symbol).toUpperCase() : "";
  const [live, setLive] = useState<number | null>(() => (sym ? _store.get(sym)?.mid ?? null : null));

  useEffect(() => {
    setLive(sym ? _store.get(sym)?.mid ?? null : null);
    if (!sym) return;
    return _subscribe(sym, (q) => setLive(q.mid));
  }, [sym]);

  if (live != null) return live;
  const fb = fallback == null ? null : Number(fallback);
  return fb != null && Number.isFinite(fb) ? fb : null;
}

/** Full live quote (mid/bid/ask) for `symbol`, or null until a tick arrives.
 *  Used by the trade panel's bid/mid/ask panel. */
export function useLiveQuote(symbol: string | null | undefined): LiveQuote | null {
  const sym = symbol ? String(symbol).toUpperCase() : "";
  const [q, setQ] = useState<LiveQuote | null>(() => (sym ? _store.get(sym) ?? null : null));

  useEffect(() => {
    setQ(sym ? _store.get(sym) ?? null : null);
    if (!sym) return;
    return _subscribe(sym, (next) => setQ(next));
  }, [sym]);

  return sym ? q : null;
}

/** Read a symbol's live mid without subscribing. For aggregate consumers that
 *  subscribe to a whole set via useLivePrices() and then read each value. */
export function peekLivePrice(symbol: string | null | undefined): number | null {
  const sym = symbol ? String(symbol).toUpperCase() : "";
  return sym ? _store.get(sym)?.mid ?? null : null;
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
    const unsubs = syms.map((sym) => _subscribe(sym, bump));
    return () => { unsubs.forEach((u) => u()); };
  }, [key]);
  return version;
}
