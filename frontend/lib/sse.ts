"use client";

import { useEffect, useRef, useState } from "react";
import { getAccessToken } from "@/lib/api";
import { applyPriceTick } from "@/lib/livePrices";

export type AppEvent =
  | { type: "order.placed"; order: OrderEventPayload }
  | { type: "order.copy_submitted"; order: OrderEventPayload }
  | { type: "order.copy_failed"; order: OrderEventPayload }
  | { type: "order.copy_retry_scheduled"; order: OrderEventPayload }
  | { type: "order.cancelled"; order: OrderEventPayload }
  /** An EXISTING order's terms changed — the Discord +10% retry, or an
   *  alert the author edited in place ("@0.20" becomes "@0.15"). Not a new
   *  order: the same row moves. Without this the table kept showing the old
   *  limit until a reload, so a moved order and an edit that never applied
   *  looked identical. The payload carries the new terms. */
  | { type: "order.updated"; order: OrderEventPayload }
  | { type: "listener.state_changed"; trader_id: string; status: ListenerStatus }
  | { type: "notification.created"; notification: NotificationEventPayload }
  /** Fired by pnl_poller when the per-position TP/SL enforcer closes
   *  one of the subscriber's open positions at market. `pct` is the
   *  unrealized P&L percent at trigger (already rounded to 2 decimals
   *  by the backend); `leg` is "tp" or "sl"; `position_tp_pct` /
   *  `position_sl_pct` are the configured thresholds. */
  | {
      type: "position.auto_closed";
      leg: "tp" | "sl";
      symbol: string;
      qty: string;
      pct: string;
      position_tp_pct: string | null;
      position_sl_pct: string | null;
      broker: string;
    }
  /** Fired by the Discord listener intake when new alerts land for this
   *  trader. Drives the live refresh of Order History's Discord tab. */
  | { type: "discord.message_received"; source_id: string; count: number }
  /** Connection state of one Discord source changed. */
  | { type: "discord.source_status"; source_id: string; status: string; error: string | null }
  /** Fired when an admin toggles a trader's Sell-All access — the trader's
   *  client re-fetches /me so the suite shows/hides live. */
  | { type: "access.sell_all_changed"; enabled: boolean };

export interface NotificationEventPayload {
  id: string;
  type: string;
  message: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ListenerStatus {
  state: "connecting" | "connected" | "reconnecting" | "disconnected" | "credentials_invalid" | "no_trader" | "no_broker"
    // Webull: the app key's one live stream is held by another environment/app.
    | "in_use_elsewhere";
  last_event_at: string | null;
  state_changed_at: string | null;
  last_error: string | null;
}

export interface OrderEventPayload {
  id: string;
  parent_order_id: string | null;
  // Nullable: orders survive when their broker is disconnected.
  broker_account_id: string | null;
  symbol: string;
  side: string;
  order_type: string;
  quantity: string;
  // Order terms — present so a broker-side MODIFY reflects instantly.
  limit_price?: string | null;
  stop_price?: string | null;
  filled_quantity: string;
  filled_avg_price: string | null;
  status: string;
  broker_order_id: string | null;
  instrument_type: string;
  // Option fields — let Call/Put + Expiry columns render on arrival.
  option_expiry?: string | null;
  option_strike?: string | null;
  option_right?: "call" | "put" | null;
  created_at: string | null;
  reject_reason: string | null;
}

/** Lifecycle state of the SSE connection itself (distinct from the
 *  broker `ListenerStatus` above, which is *about the trader's broker*). */
export type SseState =
  | "connecting"      // first open in flight
  | "connected"       // open and receiving (or at least not errored)
  | "reconnecting"    // had a transient error, scheduled reopen
  | "disconnected"    // clean unmount or shutdown
  | "unauthorized";   // 401 — caller must re-login

export interface SseStatus {
  state: SseState;
  /** Wall-clock ISO timestamp of the most recent message received, ever.
   *  Null until the first message arrives. The AppShell pill hides itself
   *  when this is recent so the UI stays quiet during normal operation. */
  lastEventAt: string | null;
}

// Backoff tuning. Exponential with 20% jitter, capped so a multi-hour
// outage doesn't push the next try too far out.
const BASE_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;
// If we see 3 errors within this window, treat as "the server is rejecting
// us" (most likely 401) instead of a network blip.
const UNAUTH_BURST_WINDOW_MS = 5_000;
const UNAUTH_BURST_COUNT = 3;
// Force-reconnect if the stream has been "connected" but silent for this
// long. Keeps a half-open TCP from looking healthy forever.
const STALE_MS = 90_000;

// ── One shared stream per tab ───────────────────────────────────────────────
// Every component that calls useEventStream used to open its OWN EventSource.
// The dashboard alone (shell + bell + listener pill + positions table + follow
// panel) was four or five live streams per tab, and Chrome caps HTTP/1.1 at
// six connections per host. Two tabs were enough to fill the pool against the
// Next dev proxy, after which EVERY other request to localhost:3000 queued
// behind the streams and the app sat on "loading" forever. A module-level
// manager now owns ONE connection per tab; hook callers subscribe to it, and
// only the first subscriber opens the socket / the last one closes it.

type EventListener = (e: AppEvent) => void;
type StatusListener = (s: SseStatus) => void;

const eventListeners = new Set<EventListener>();
const statusListeners = new Set<StatusListener>();
let status: SseStatus = { state: "disconnected", lastEventAt: null };
let es: EventSource | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let staleTimer: ReturnType<typeof setInterval> | null = null;
let closeTimer: ReturnType<typeof setTimeout> | null = null;
let attempt = 0;
// Sliding-window error timestamps for unauthorized detection.
let recentErrors: number[] = [];
// Tracked here so the stale-check interval reads the latest value without
// going through React state (which lags one render).
let lastEventAtMs = 0;
let running = false;

function setStatus(patch: Partial<SseStatus>) {
  status = { ...status, ...patch };
  statusListeners.forEach((l) => l(status));
}

function jitter(ms: number): number {
  // ±20% jitter so multiple tabs / users don't synchronize their
  // reconnect storms.
  const spread = ms * 0.2;
  return ms + (Math.random() * 2 - 1) * spread;
}

function scheduleReconnect() {
  if (!running) return;
  attempt += 1;
  const delay = Math.min(
    BASE_BACKOFF_MS * Math.pow(2, attempt - 1),
    MAX_BACKOFF_MS,
  );
  const withJitter = Math.max(0, jitter(delay));
  setStatus({ state: "reconnecting" });
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    open();
  }, withJitter);
}

function open() {
  if (!running) return;
  // Always close any prior stream before opening a new one. EventSource
  // doesn't surface a leak if you forget, but it does keep the old
  // connection's onmessage live and you'll see duplicate events.
  if (es) {
    try { es.close(); } catch { /* ignore */ }
    es = null;
  }
  const tok = getAccessToken();
  if (!tok) {
    setStatus({ state: "unauthorized" });
    return;
  }
  // Auth via query-param token because EventSource can't set headers.
  const url = `/api/events?token=${encodeURIComponent(tok)}`;
  const next = new EventSource(url);
  es = next;
  setStatus({ state: "connecting" });

  next.onopen = () => {
    // We're back. Reset the backoff and the error-burst window.
    attempt = 0;
    recentErrors = [];
    setStatus({ state: "connected" });
  };

  next.onmessage = (msg) => {
    lastEventAtMs = Date.now();
    // Surface as "connected" the moment we get any payload — onopen
    // doesn't fire across every browser before the first message.
    setStatus({ state: "connected", lastEventAt: new Date().toISOString() });
    let evt: AppEvent;
    try {
      evt = JSON.parse(msg.data) as AppEvent;
    } catch {
      return; // ignore malformed events
    }
    // Live price ticks feed the shared price store for the whole app;
    // they're not page events, so handle them here and don't forward.
    if ((evt as { type?: string }).type === "price.tick") {
      applyPriceTick(evt as { symbol?: string; price?: string | number });
      return;
    }
    eventListeners.forEach((l) => {
      try { l(evt); } catch { /* one bad handler must not starve the rest */ }
    });
  };

  next.onerror = () => {
    // EventSource fires onerror on both transient drops and hard
    // failures. Only act when readyState === CLOSED so we don't
    // double-reconnect during the browser's own auto-retry.
    if (next.readyState !== EventSource.CLOSED) return;

    const now = Date.now();
    recentErrors.push(now);
    recentErrors = recentErrors.filter((t) => now - t < UNAUTH_BURST_WINDOW_MS);
    if (recentErrors.length >= UNAUTH_BURST_COUNT) {
      // Server keeps closing us right after we connect — almost
      // certainly a 401. Stop the storm and let the user re-login.
      setStatus({ state: "unauthorized" });
      try { next.close(); } catch { /* ignore */ }
      es = null;
      return;
    }

    try { next.close(); } catch { /* ignore */ }
    es = null;
    scheduleReconnect();
  };
}

function start() {
  if (running) return;
  running = true;
  if (!getAccessToken()) {
    // Not signed in. Leave the manager stopped so the next subscriber
    // (mounted after login) gets a fresh attempt instead of a dead stream.
    running = false;
    setStatus({ state: "disconnected" });
    return;
  }
  open();

  // Stale-connection watchdog. If we've been "connected" for a while
  // but haven't received anything in STALE_MS, the TCP is probably
  // half-open. Force a reconnect — cheaper than waiting for the OS
  // to notice the dead socket. Disabled while we're already in a
  // reconnect cycle to avoid stacking.
  staleTimer = setInterval(() => {
    if (!running) return;
    if (lastEventAtMs === 0) return; // never connected — let backoff drive
    const since = Date.now() - lastEventAtMs;
    if (since > STALE_MS && es && es.readyState === EventSource.OPEN) {
      // Treat as a transient error — close + reopen via scheduleReconnect
      // so we hit the same jitter/backoff path.
      try { es.close(); } catch { /* ignore */ }
      es = null;
      scheduleReconnect();
    }
  }, 15_000);
}

function stop() {
  running = false;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (staleTimer) { clearInterval(staleTimer); staleTimer = null; }
  if (es) {
    try { es.close(); } catch { /* ignore */ }
    es = null;
  }
  attempt = 0;
  recentErrors = [];
  lastEventAtMs = 0;
  setStatus({ state: "disconnected" });
}

function subscribe(onEvent: EventListener, onStatus: StatusListener): () => void {
  eventListeners.add(onEvent);
  statusListeners.add(onStatus);
  if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }
  if (!running) start();
  onStatus(status);
  return () => {
    eventListeners.delete(onEvent);
    statusListeners.delete(onStatus);
    if (eventListeners.size === 0 && !closeTimer) {
      // Deferred so a React Strict Mode remount, or a route change that
      // swaps one subscriber for another, reuses the socket instead of
      // closing and reopening it.
      closeTimer = setTimeout(() => {
        closeTimer = null;
        if (eventListeners.size === 0) stop();
      }, 250);
    }
  };
}

/**
 * Subscribe to the server's per-user SSE stream with automatic reconnection.
 *
 * All callers in a tab share ONE underlying EventSource (see the manager
 * above); each gets every event and the live connection status. Returns the
 * status so the AppShell can render a "Reconnecting…" pill. Callers that
 * ignore the return value keep working — `useEventStream(onEvent)` is still
 * a valid call shape.
 */
export function useEventStream(
  onEvent: (e: AppEvent) => void,
): SseStatus {
  // Stash the handler in a ref so we never re-subscribe just because the
  // caller passed a new function literal.
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  const [current, setCurrent] = useState<SseStatus>(() => status);

  useEffect(() => {
    const listener: EventListener = (e) => handlerRef.current(e);
    return subscribe(listener, setCurrent);
  }, []);

  return current;
}
