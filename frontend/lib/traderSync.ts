/** Same-tab cross-component sync via lightweight window events.
 *
 * Different components hold their own copies of some state, so a change in one
 * wouldn't reflect in another until a refresh. These broadcast a window event
 * the other side subscribes to — no server round-trip, no shared store.
 * Covers: the trader's Discord-alert state, and broker connect/disconnect (so
 * the navbar broker-status pill updates without waiting for its poll). */
export const TRADER_DISCORD_CHANGED = "trader:discord-changed";

export interface DiscordChangedDetail {
  enabled: boolean;
  configured: boolean;
}

export function emitDiscordChanged(detail: DiscordChangedDetail): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(TRADER_DISCORD_CHANGED, { detail }));
}

/** Subscribe; returns an unsubscribe fn for a useEffect cleanup. */
export function onDiscordChanged(cb: (d: DiscordChangedDetail) => void): () => void {
  const handler = (e: Event) => {
    const d = (e as CustomEvent).detail;
    if (d && typeof d.enabled === "boolean" && typeof d.configured === "boolean") cb(d);
  };
  window.addEventListener(TRADER_DISCORD_CHANGED, handler);
  return () => window.removeEventListener(TRADER_DISCORD_CHANGED, handler);
}

/** The user connected / disconnected a broker on the Brokers page. The navbar
 *  broker-status pill lives in a different component and otherwise only picks up
 *  the change on its 30s poll (looks "stuck until refresh"), so the Brokers page
 *  fires this and the pill re-fetches immediately. Payload-free — subscribers
 *  just re-pull the current status. */
export const BROKER_CHANGED = "broker:changed";

export function emitBrokerChanged(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(BROKER_CHANGED));
}

/** Subscribe; returns an unsubscribe fn for a useEffect cleanup. */
export function onBrokerChanged(cb: () => void): () => void {
  const handler = () => cb();
  window.addEventListener(BROKER_CHANGED, handler);
  return () => window.removeEventListener(BROKER_CHANGED, handler);
}
