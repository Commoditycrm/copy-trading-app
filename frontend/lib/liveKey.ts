import type { Order } from "@/lib/types";

/** Live-cache key for an order's symbol — the ticker for stocks, the OCC for
 *  options (matching the stream/backend format). Null when we can't build one. */
export function orderLiveKey(o: Order): string | null {
  const sym = (o.symbol || "").toUpperCase();
  if (!sym || sym === "—") return null;
  if (o.instrument_type !== "option") return sym;
  if (!o.option_expiry || o.option_strike == null || o.option_strike === "" || !o.option_right) return null;
  const iso = o.option_expiry;
  const d = new Date(iso.length === 10 ? iso + "T00:00:00Z" : iso);
  if (Number.isNaN(d.getTime())) return null;
  const yy = String(d.getUTCFullYear()).slice(-2);
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const cp = o.option_right === "call" ? "C" : "P";
  const strikeInt = Math.round(Number(o.option_strike) * 1000);
  if (!Number.isFinite(strikeInt)) return null;
  return `${sym}${yy}${mm}${dd}${cp}${String(strikeInt).padStart(8, "0")}`;
}
