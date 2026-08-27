/** Same-tab cross-component sync for the trader's Discord-alert state.
 *
 * The sidebar copy-toggle popup (AppShell) and the Settings page each hold their
 * own copy of the trader settings, so a change in one wouldn't reflect in the
 * other until a refresh. This broadcasts a lightweight window event both sides
 * emit on change and subscribe to — no server round-trip, no shared store. */
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
