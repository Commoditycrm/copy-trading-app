"use client";

import { useEffect, useRef, useState } from "react";
import { getAccessToken } from "@/lib/api";

export type AppEvent =
  | { type: "order.placed"; order: OrderEventPayload }
  | { type: "order.copy_submitted"; order: OrderEventPayload }
  | { type: "order.copy_failed"; order: OrderEventPayload }
  | { type: "order.copy_retry_scheduled"; order: OrderEventPayload }
  | { type: "order.cancelled"; order: OrderEventPayload }
  | { type: "listener.state_changed"; trader_id: string; status: ListenerStatus }
  | { type: "notification.created"; notification: NotificationEventPayload };

export interface NotificationEventPayload {
  id: string;
  type: string;
  message: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ListenerStatus {
  state: "connecting" | "connected" | "reconnecting" | "disconnected" | "credentials_invalid" | "no_trader";
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
  filled_quantity: string;
  filled_avg_price: string | null;
  status: string;
  broker_order_id: string | null;
  instrument_type: string;
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

/**
 * Subscribe to the server's per-user SSE stream with automatic reconnection.
 *
 * Returns the connection status so the AppShell can render a "Reconnecting…"
 * pill. Existing callers that ignore the return value keep working —
 * `useEventStream(onEvent)` is still a valid call shape.
 *
 * Auth via query-param token because EventSource can't set headers.
 */
export function useEventStream(
  onEvent: (e: AppEvent) => void,
): SseStatus {
  // Stash the handler in a ref so we never re-open the stream just because
  // the caller passed a new function literal.
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  const [state, setState] = useState<SseState>("connecting");
  const [lastEventAt, setLastEventAt] = useState<string | null>(null);

  useEffect(() => {
    const token = getAccessToken();
    if (!token) {
      setState("disconnected");
      return;
    }

    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let staleTimer: ReturnType<typeof setInterval> | null = null;
    let attempt = 0;
    let cancelled = false;
    // Sliding-window error timestamps for unauthorized detection.
    let recentErrors: number[] = [];
    // Tracked locally so the stale-check interval can read the latest
    // value without going through React state (which lags one render).
    let lastEventAtMs = 0;

    function jitter(ms: number): number {
      // ±20% jitter so multiple tabs / users don't synchronize their
      // reconnect storms.
      const spread = ms * 0.2;
      return ms + (Math.random() * 2 - 1) * spread;
    }

    function scheduleReconnect() {
      if (cancelled) return;
      attempt += 1;
      const delay = Math.min(
        BASE_BACKOFF_MS * Math.pow(2, attempt - 1),
        MAX_BACKOFF_MS,
      );
      const withJitter = Math.max(0, jitter(delay));
      setState("reconnecting");
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        open();
      }, withJitter);
    }

    function open() {
      // Always close any prior stream before opening a new one. EventSource
      // doesn't surface a leak if you forget, but it does keep the old
      // connection's onmessage live and you'll see duplicate events.
      if (es) {
        try { es.close(); } catch { /* ignore */ }
        es = null;
      }
      const tok = getAccessToken();
      if (!tok) {
        setState("unauthorized");
        return;
      }
      const url = `/api/events?token=${encodeURIComponent(tok)}`;
      const next = new EventSource(url);
      es = next;
      setState("connecting");

      next.onopen = () => {
        // We're back. Reset the backoff and the error-burst window.
        attempt = 0;
        recentErrors = [];
        setState("connected");
      };

      next.onmessage = (msg) => {
        const nowIso = new Date().toISOString();
        lastEventAtMs = Date.now();
        setLastEventAt(nowIso);
        // Surface as "connected" the moment we get any payload — onopen
        // doesn't fire across every browser before the first message.
        setState("connected");
        try {
          const evt = JSON.parse(msg.data) as AppEvent;
          handlerRef.current(evt);
        } catch {
          /* ignore malformed events */
        }
      };

      next.onerror = () => {
        // EventSource fires onerror on both transient drops and hard
        // failures. Only act when readyState === CLOSED so we don't
        // double-reconnect during the browser's own auto-retry.
        if (next.readyState !== EventSource.CLOSED) return;

        const now = Date.now();
        recentErrors.push(now);
        recentErrors = recentErrors.filter(t => now - t < UNAUTH_BURST_WINDOW_MS);
        if (recentErrors.length >= UNAUTH_BURST_COUNT) {
          // Server keeps closing us right after we connect — almost
          // certainly a 401. Stop the storm and let the user re-login.
          setState("unauthorized");
          try { next.close(); } catch { /* ignore */ }
          es = null;
          return;
        }

        try { next.close(); } catch { /* ignore */ }
        es = null;
        scheduleReconnect();
      };
    }

    open();

    // Stale-connection watchdog. If we've been "connected" for a while
    // but haven't received anything in STALE_MS, the TCP is probably
    // half-open. Force a reconnect — cheaper than waiting for the OS
    // to notice the dead socket. Disabled while we're already in a
    // reconnect cycle to avoid stacking.
    staleTimer = setInterval(() => {
      if (cancelled) return;
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

    return () => {
      cancelled = true;
      setState("disconnected");
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (staleTimer) clearInterval(staleTimer);
      if (es) {
        try { es.close(); } catch { /* ignore */ }
        es = null;
      }
    };
  }, []);

  return { state, lastEventAt };
}
